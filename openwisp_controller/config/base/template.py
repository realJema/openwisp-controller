from collections import OrderedDict
from copy import copy

from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import ugettext_lazy as _
from jsonfield import JSONField
from swapper import get_model_name
from taggit.managers import TaggableManager

from openwisp_users.mixins import ShareableOrgMixin

from ..settings import DEFAULT_AUTO_CERT
from .base import BaseConfig

TYPE_CHOICES = (
    ('generic', _('Generic')),
    ('vpn', _('VPN-client')),
)


def default_auto_cert():
    """
    returns the default value for auto_cert field
    (this avoids to set the exact default value in the database migration)
    """
    return DEFAULT_AUTO_CERT


class AbstractTemplate(ShareableOrgMixin, BaseConfig):
    """
    Abstract model implementing a
    netjsonconfig template
    """

    tags = TaggableManager(
        through=get_model_name('config', 'TaggedTemplate'),
        blank=True,
        help_text=_(
            'A comma-separated list of template tags, may be used '
            'to ease auto configuration with specific settings (eg: '
            '4G, mesh, WDS, VPN, ecc.)'
        ),
    )
    vpn = models.ForeignKey(
        get_model_name('config', 'Vpn'),
        verbose_name=_('VPN'),
        blank=True,
        null=True,
        on_delete=models.CASCADE,
    )
    type = models.CharField(
        _('type'),
        max_length=16,
        choices=TYPE_CHOICES,
        default='generic',
        db_index=True,
        help_text=_('template type, determines which ' 'features are available'),
    )
    default = models.BooleanField(
        _('enabled by default'),
        default=False,
        db_index=True,
        help_text=_(
            'whether new configurations will have ' 'this template enabled by default'
        ),
    )
    auto_cert = models.BooleanField(
        _('auto certificate'),
        default=default_auto_cert,
        db_index=True,
        help_text=_(
            'whether x509 client certificates should '
            'be automatically managed behind the scenes '
            'for each configuration using this template, '
            'valid only for the VPN type'
        ),
    )
    default_values = JSONField(
        _('Default Values'),
        default=dict,
        blank=True,
        help_text=_(
            'A dictionary containing the default '
            'values for the variables used by this '
            'template; these default variables will '
            'be used during schema validation.'
        ),
        load_kwargs={'object_pairs_hook': OrderedDict},
        dump_kwargs={'indent': 4},
    )
    __template__ = True

    class Meta:
        abstract = True
        verbose_name = _('template')
        verbose_name_plural = _('templates')
        unique_together = (('organization', 'name'),)

    def save(self, *args, **kwargs):
        """
        modifies status of related configs
        if key attributes have changed (queries the database)
        """
        update_related_config_status = False
        if not self._state.adding:
            current = self.__class__.objects.get(pk=self.pk)
            for attr in ['backend', 'config']:
                if getattr(self, attr) != getattr(current, attr):
                    update_related_config_status = True
                    break
        # save current changes
        super().save(*args, **kwargs)
        # update relations
        if update_related_config_status:
            self._update_related_config_status()

    def _update_related_config_status(self):
        changing_status = list(self.config_relations.exclude(status='modified'))
        self.config_relations.update(status='modified')
        for config in self.config_relations.all():
            # config modified signal sent regardless
            config._send_config_modified_signal()
            # config status changed signal sent only if status changed
            if config in changing_status:
                config._send_config_status_changed_signal()

    def clean(self, *args, **kwargs):
        """
        * validates org relationship of VPN if present
        * validates default_values field
        * ensures VPN is selected if type is VPN
        * clears VPN specific fields if type is not VPN
        * automatically determines configuration if necessary
        """
        self._validate_org_relation('vpn')
        if not self.default_values:
            self.default_values = {}
        if not isinstance(self.default_values, dict):
            raise ValidationError(
                {'default_values': _('the supplied value is not a JSON object')}
            )
        if self.type == 'vpn' and not self.vpn:
            raise ValidationError(
                {'vpn': _('A VPN must be selected when template type is "VPN"')}
            )
        elif self.type != 'vpn':
            self.vpn = None
            self.auto_cert = False
        if self.type == 'vpn' and not self.config:
            self.config = self.vpn.auto_client(auto_cert=self.auto_cert)
        super().clean(*args, **kwargs)

    def get_context(self):
        context = {}
        if self.default_values:
            context = copy(self.default_values)
        context.update(super().get_context())
        return context

    def clone(self, user):
        clone = copy(self)
        clone.name = self.__get_clone_name()
        clone._state.adding = True
        clone.pk = None
        # avoid cloned templates to be flagged as default
        # to avoid potential unwanted duplications in
        # newly registrated devices
        clone.default = False
        clone.full_clean()
        clone.save()
        ct = ContentType.objects.get(model='template')
        LogEntry.objects.log_action(
            user_id=user.id,
            content_type_id=ct.pk,
            object_id=clone.pk,
            object_repr=clone.name,
            action_flag=ADDITION,
        )
        return clone

    def __get_clone_name(self):
        name = '{} (Clone)'.format(self.name)
        index = 2
        while self.__class__.objects.filter(name=name).count():
            name = '{} (Clone {})'.format(self.name, index)
            index += 1
        return name


AbstractTemplate._meta.get_field('config').blank = True
