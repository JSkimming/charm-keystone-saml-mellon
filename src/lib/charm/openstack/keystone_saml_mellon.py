#
# Copyright 2017 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import charmhelpers.core as core
import charmhelpers.core.host as ch_host
import charmhelpers.core.hookenv as hookenv
import charmhelpers.core.unitdata as unitdata

import charmhelpers.contrib.openstack.templating as os_templating
import charmhelpers.contrib.openstack.utils as os_utils

import charms_openstack.charm
import charms_openstack.adapters

import os
import subprocess

from lxml import etree
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

# release detection is done via keystone package given that
# openstack-origin is not present in the subordinate charm
# see https://github.com/juju/charm-helpers/issues/83
from charms_openstack.charm.core import (
    register_os_release_selector
)
OPENSTACK_RELEASE_KEY = 'charmers.openstack-release-version'

CONFIGS = (IDP_METADATA, SP_METADATA, SP_PRIVATE_KEY,
           SP_LOCATION_CONFIG,) = [
               os.path.join('/etc/apache2/mellon',
                            f.format(hookenv.service_name())) for f in [
                                'idp-meta.{}.xml',
                                'sp-meta.{}.xml',
                                'sp-pk.{}.pem',
                                'sp-location.{}.conf']]


@register_os_release_selector
def select_release():
    """Determine the release based on the keystone package version.

    Note that this function caches the release after the first install so
    that it doesn't need to keep going and getting it from the package
    information.
    """
    release_version = unitdata.kv().get(OPENSTACK_RELEASE_KEY, None)
    if release_version is None:
        release_version = os_utils.os_release('keystone')
        unitdata.kv().set(OPENSTACK_RELEASE_KEY, release_version)
    return release_version


class KeystoneSAMLMellonConfigurationAdapter(
        charms_openstack.adapters.ConfigurationAdapter):

    def __init__(self, charm_instance=None):
        super().__init__(charm_instance=charm_instance)
        self._idp_metadata = None
        self._sp_private_key = None
        self._sp_signing_keyinfo = None
        self._validation_errors = {}

    @property
    def validation_errors(self):
        return {k: v for k, v in
                self._validation_errors.items() if v}

    @property
    def remote_id_attribute(self):
        # Mellon module environment variables are prefixed with MELLON_
        # and mod_auth_mellon has a default setting of: MellonIdP "IDP"
        return "MELLON_IDP"

    @property
    def idp_metadata_file(self):
        return IDP_METADATA

    @property
    def sp_metadata_file(self):
        return SP_METADATA

    @property
    def sp_private_key_file(self):
        return SP_PRIVATE_KEY

    @property
    def sp_location_config(self):
        return SP_LOCATION_CONFIG

    @property
    def keystone_host(self):
        return unitdata.kv().get('hostname')

    @property
    def keystone_port(self):
        return unitdata.kv().get('port')

    @property
    def tls_enabled(self):
        return unitdata.kv().get('tls-enabled')

    @property
    def keystone_base_url(self):
        scheme = 'https' if self.tls_enabled else 'http'
        return ('{}://{}:{}'.format(scheme, self.keystone_host,
                                    self.keystone_port))

    @property
    def sp_idp_path(self):
        return ('/v3/OS-FEDERATION/identity_providers/{}'
                .format(self.idp_name))

    @property
    def sp_protocol_path(self):
        return ('{}/protocols/{}'
                .format(self.sp_idp_path, self.protocol_name))

    @property
    def sp_auth_path(self):
        return '{}/auth'.format(self.sp_protocol_path)

    @property
    def mellon_endpoint_path(self):
        return '{}/mellon'.format(self.sp_auth_path)

    @property
    def websso_auth_protocol_path(self):
        return ('/v3/auth/OS-FEDERATION/websso/{}'
                .format(self.protocol_name))

    @property
    def websso_auth_idp_protocol_path(self):
        return ('/v3/auth/OS-FEDERATION/identity_providers'
                '/{}/protocols/{}/websso'.format(
                    self.idp_name,
                    self.protocol_name
                ))

    @property
    def sp_post_response_path(self):
        return '{}/postResponse'.format(self.mellon_endpoint_path)

    @property
    def sp_logout_path(self):
        return '{}/logout'.format(self.mellon_endpoint_path)

    @property
    def sp_auth_url(self):
        return '{}{}'.format(self.keystone_base_url,
                             self.sp_auth_path)

    @property
    def sp_logout_url(self):
        return '{}{}'.format(self.keystone_base_url,
                             self.sp_logout_path)

    @property
    def sp_post_response_url(self):
        return '{}{}'.format(self.keystone_base_url,
                             self.sp_post_response_path)

    @property
    def mellon_subject_confirmation_data_address_check(self):
        return ('On' if self.subject_confirmation_data_address_check
                else 'Off')

    @property
    def supported_nameid_formats(self):
        return self.nameid_formats.split(',')

    IDP_METADATA_INVALID = ('idp-metadata resource is not a well-formed'
                            ' xml file')

    @property
    def idp_metadata(self):
        idp_metadata_path = hookenv.resource_get('idp-metadata')
        if os.path.exists(idp_metadata_path) and not self._idp_metadata:
            with open(idp_metadata_path) as f:
                content = f.read()
                try:
                    etree.fromstring(content.encode())
                    self._idp_metadata = content
                    self._validation_errors['idp-metadata'] = None
                except etree.XMLSyntaxError:
                    self._idp_metadata = ''
                    self._validation_errors['idp-metadata'] = (
                        self.IDP_METADATA_INVALID)
        return self._idp_metadata

    SP_SIGNING_KEYINFO_INVALID = ('sp-signing-keyinfo resource is not a'
                                  ' well-formed xml file')

    @property
    def sp_signing_keyinfo(self):
        info_path = hookenv.resource_get('sp-signing-keyinfo')
        if os.path.exists(info_path) and not self._sp_signing_keyinfo:
            self._sp_signing_keyinfo = None
            with open(info_path) as f:
                content = f.read()
                try:
                    etree.fromstring(content.encode())
                    self._sp_signing_keyinfo = content
                    self._validation_errors['sp-signing-keyinfo'] = None
                except etree.XMLSyntaxError:
                    self._sp_signing_keyinfo = ''
                    self._validation_errors['sp-signing-keyinfo'] = (
                        self.SP_SIGNING_KEYINFO_INVALID)
        return self._sp_signing_keyinfo

    SP_PRIVATE_KEY_INVALID = ('resource is not a well-formed'
                              ' RFC 5958 (PKCS#8) key')

    @property
    def sp_private_key(self):
        pk_path = hookenv.resource_get('sp-private-key')
        if os.path.exists(pk_path) and not self._sp_private_key:
            with open(pk_path) as f:
                content = f.read()
                try:
                    serialization.load_pem_private_key(
                        content.encode(),
                        password=None,
                        backend=default_backend()
                    )
                    self._sp_private_key = content
                    self._validation_errors['sp-private-key'] = None
                except ValueError:
                    self._sp_private_key = ''
                    self._validation_errors['sp-private-key'] = (
                        self.SP_PRIVATE_KEY_INVALID)
        return self._sp_private_key


class KeystoneSAMLMellonCharm(charms_openstack.charm.OpenStackCharm):

    # Internal name of charm
    service_name = name = 'keystone-saml-mellon'

    # Package to derive application version from
    version_package = 'keystone'

    # First release supported
    release = 'mitaka'

    # List of packages to install for this charm
    packages = ['libapache2-mod-auth-mellon']

    configuration_class = KeystoneSAMLMellonConfigurationAdapter

    # render idP metadata provided out of band to establish
    # SP -> idP trust. A domain name config parameter is evaluated at
    # class definition time but this happens every event execution,
    # including config-changed. Changing domain-name dynamically is not
    # a real use-case anyway and it should be defined deployment time.
    string_templates = {
        IDP_METADATA: ('options', 'idp_metadata'),
        SP_PRIVATE_KEY: ('options', 'sp_private_key'),
    }

    # Render idp-metadata.xml and sp-private-key with www-data group
    # ownership.
    group = 'www-data'

    def configuration_complete(self):
        """Determine whether sufficient configuration has been provided
        via charm config options and resources.
        :returns: boolean indicating whether configuration is complete
        """
        required_config = {
            'idp-name': self.options.idp_name,
            'protocol-name': self.options.protocol_name,
            'user-facing-name': self.options.user_facing_name,
            'idp-metadata': self.options.idp_metadata,
            'sp-private-key': self.options.sp_private_key,
            'sp-signing-keyinfo': self.options.sp_signing_keyinfo,
            'nameid-formats': self.options.nameid_formats,
        }

        return all(required_config.values())

    def assess_status(self):
        """Determine the current application status for the charm"""
        hookenv.application_version_set(self.application_version)
        if not self.configuration_complete():
            errors = [
                '{}: {}'.format(k, v)
                for k, v in self.options.validation_errors.items() if v]
            status_msg = 'Configuration is incomplete. {}'.format(
                ','.join(errors))
            hookenv.status_set('blocked', status_msg)
        else:
            hookenv.status_set('active',
                               'Unit is ready')

    def render_config(self):
        """
        Render Service Provider configuration file to be used by Apache
        and provided to idP out of band to establish mutual trust.
        """
        owner = 'root'
        group = 'www-data'
        # group read and exec is needed for mellon to read the rendered
        # files, otherwise it will fail in a cryptic way
        dperms = 0o650
        # file permissions are a bit more restrictive than defaults in
        # charm-helpers but directory permissions are the main protection
        # mechanism in this case
        fileperms = 0o440
        # ensure that a directory we need is there
        ch_host.mkdir('/etc/apache2/mellon', perms=dperms, owner=owner,
                      group=group)
        self.render_configs(self.string_templates.keys())

        core.templating.render(
            source='mellon-sp-metadata.xml',
            template_loader=os_templating.get_loader(
                'templates/', self.release),
            target=self.options.sp_metadata_file,
            context=self.adapters_instance,
            owner=owner,
            group=group,
            perms=fileperms
        )

        core.templating.render(
            source='apache-mellon-location.conf',
            template_loader=os_templating.get_loader(
                'templates/', self.release),
            target=self.options.sp_location_config,
            context=self.adapters_instance,
            owner=owner,
            group=group,
            perms=fileperms
        )

    def remove_config(self):
        for f in CONFIGS:
            if os.path.exists(f):
                os.unlink(f)

    def enable_module(self):
        subprocess.check_call(['a2enmod', 'auth_mellon'])

    def disable_module(self):
        subprocess.check_call(['a2dismod', 'auth_mellon'])
