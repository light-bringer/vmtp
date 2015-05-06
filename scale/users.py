# Copyright 2015 Cisco Systems, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import base_compute
import base_network
import keystoneclient.openstack.common.apiclient.exceptions as keystone_exception
import log as logging
from neutronclient.v2_0 import client as neutronclient
from novaclient.client import Client

LOG = logging.getLogger(__name__)


class User(object):
    """
    User class that stores router list
    Creates and deletes N routers based on num of routers
    """

    def __init__(self, user_name, tenant, user_role):
        """
        Store all resources
        1. Keystone client object
        2. Tenant and User information
        3. nova and neutron clients
        4. router list
        """
        self.user_name = user_name
        self.tenant = tenant
        self.user_id = None
        self.router_list = []
        # Store the neutron and nova client
        self.neutron_client = None
        self.nova_client = None
        self.admin_user = self._get_user()
        # Each user is associated to 1 key pair at most
        self.key_pair = None
        self.key_name = None

        # Create the user within the given tenant associate
        # admin role with user. We need admin role for user
        # since we perform VM placement in future

        current_role = None
        for role in self.tenant.kloud.keystone.roles.list():
            if role.name == user_role:
                current_role = role
                break
        self.tenant.kloud.keystone.roles.add_user_role(self.admin_user,
                                                       current_role,
                                                       tenant.tenant_id)
        self.user_id = self.admin_user.id

    def _create_user(self):
        LOG.info("Creating user: " + self.user_name)
        return self.tenant.kloud.keystone.users.create(name=self.user_name,
                                                       password=self.user_name,
                                                       email="kloudbuster@localhost",
                                                       tenant_id=self.tenant.tenant_id)

    def _get_user(self):
        '''
        Create a new user or reuse if it already exists (on a different tenant)
        delete the user and create a new one
        '''
        try:
            user = self._create_user()
            return user
        except keystone_exception.Conflict as exc:
            # Most likely the entry already exists (leftover from past failed runs):
            # Conflict: Conflict occurred attempting to store user - Duplicate Entry (HTTP 409)
            if exc.http_status != 409:
                raise exc
            # Try to repair keystone by removing that user
            LOG.warn("User creation failed due to stale user with same name: " +
                     self.user_name)
            # Again, trying to find a user by name is pretty inefficient as one has to list all
            # of them
            users_list = self.tenant.kloud.keystone.users.list()
            for user in users_list:
                if user.name == self.user_name:
                    # Found it, time to delete it
                    LOG.info("Deleting stale user with name: " + self.user_name)
                    self.tenant.kloud.keystone.users.delete(user)
                    user = self._create_user()
                    return user

        # Not found there is something wrong
        raise Exception('Cannot find stale user:' + self.user_name)

    def delete_resources(self):
        LOG.info("Deleting all user resources for user %s" % self.user_name)

        # Delete key pair
        if self.key_pair:
            self.key_pair.remove_public_key()

        # Delete all user routers
        for router in self.router_list:
            router.delete_router()

        # Finally delete the user
        self.tenant.kloud.keystone.users.delete(self.user_id)

    def create_resources(self):
        """
        Creates all the User elements associated with a User
        1. Creates the routers
        2. Creates the neutron and nova client objects
        """
        # Create a new neutron client for this User with correct credentials
        creden = {}
        creden['username'] = self.user_name
        creden['password'] = self.user_name
        creden['auth_url'] = self.tenant.kloud.auth_url
        creden['tenant_name'] = self.tenant.tenant_name

        # Create the neutron client to be used for all operations
        self.neutron_client = neutronclient.Client(**creden)

        # Create a new nova client for this User with correct credentials
        creden_nova = {}
        creden_nova['username'] = self.user_name
        creden_nova['api_key'] = self.user_name
        creden_nova['auth_url'] = self.tenant.kloud.auth_url
        creden_nova['project_id'] = self.tenant.tenant_name
        creden_nova['version'] = 2
        self.nova_client = Client(**creden_nova)

        config_scale = self.tenant.kloud.scale_cfg

        # Create the user's keypair if configured
        if config_scale.public_key_file:
            self.key_pair = base_compute.KeyPair(self.nova_client)
            self.key_name = self.user_name + '-K'
            self.key_pair.add_public_key(self.key_name, config_scale.public_key_file)

        # Find the external network that routers need to attach to
        # if redis_server is configured, we need to attach the router to the
        # external network in order to reach the redis_server
        if config_scale['use_floatingip'] or 'redis_server' in config_scale:
            external_network = base_network.find_external_network(self.neutron_client)
        else:
            external_network = None

        # Create the required number of routers and append them to router list
        LOG.info("Creating routers and networks for user %s" % self.user_name)
        for router_count in range(config_scale['routers_per_user']):
            router_instance = base_network.Router(self)
            self.router_list.append(router_instance)
            router_name = self.user_name + "-R" + str(router_count)
            # Create the router and also attach it to external network
            router_instance.create_router(router_name, external_network)
            # Now create the network resources inside the router
            router_instance.create_network_resources(config_scale)

    def get_first_network(self):
        if self.router_list:
            return self.router_list[0].get_first_network()
        return None

    def get_all_instances(self):
        all_instances = []
        for router in self.router_list:
            all_instances.extend(router.get_all_instances())
        return all_instances
