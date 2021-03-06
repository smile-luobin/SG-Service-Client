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

"""
Command-line interface to the SG-Service Project.
"""

from __future__ import print_function

import argparse
import sys

from keystoneclient.auth.identity.generic import password
from keystoneclient.auth.identity.generic import token
from keystoneclient.auth.identity import v3 as identity
from keystoneclient import discover
from keystoneclient import exceptions as ks_exc
from keystoneclient import session as ksession
from oslo_log import handlers
from oslo_log import log as logging
from oslo_utils import encodeutils
from oslo_utils import importutils

import six
import six.moves.urllib.parse as urlparse

import sgsclient
from sgsclient import client as sgs_client
from sgsclient.common import utils
from sgsclient.openstack.common.apiclient import exceptions as exc

logger = logging.getLogger(__name__)


class SGServiceShell(object):
    def _append_global_identity_args(self, parser):
        # Register the CLI arguments that have moved to the session object.
        ksession.Session.register_cli_options(parser)

        identity.Password.register_argparse_arguments(parser)

    def get_base_parser(self):

        parser = argparse.ArgumentParser(
            prog='sgs',
            description=__doc__.strip(),
            epilog='See "sgs help COMMAND" '
                   'for help on a specific command.',
            add_help=False,
            formatter_class=HelpFormatter,
        )
        # Global arguments
        parser.add_argument('-h', '--help',
                            action='store_true',
                            help=argparse.SUPPRESS, )

        parser.add_argument('--version',
                            action='version',
                            version=sgsclient.__version__,
                            help="Show program's version number and exit.")

        parser.add_argument('-d', '--debug',
                            default=bool(utils.env('SGSCLIENT_DEBUG')),
                            action='store_true',
                            help='Defaults to env[SGSCLIENT_DEBUG].')

        parser.add_argument('-v', '--verbose',
                            default=False, action="store_true",
                            help="Print more verbose output.")

        # os-cert, os-key, insecure, ca-file are all added
        # by keystone session register_cli_opts later
        parser.add_argument('--cert-file',
                            dest='os_cert',
                            help='DEPRECATED! Use --os-cert.')

        parser.add_argument('--key-file',
                            dest='os_key',
                            help='DEPRECATED! Use --os-key.')

        parser.add_argument('--ca-file',
                            dest='os_cacert',
                            help='DEPRECATED! Use --os-cacert.')

        parser.add_argument('--api-timeout',
                            help='Number of seconds to wait for an '
                                 'API response, '
                                 'defaults to system socket timeout.')

        parser.add_argument('--os-tenant-id',
                            default=utils.env('OS_TENANT_ID'),
                            help='Defaults to env[OS_TENANT_ID].')

        parser.add_argument('--os-tenant-name',
                            default=utils.env('OS_TENANT_NAME'),
                            help='Defaults to env[OS_TENANT_NAME].')

        parser.add_argument('--os-region-name',
                            default=utils.env('OS_REGION_NAME'),
                            help='Defaults to env[OS_REGION_NAME].')

        parser.add_argument('--os-auth-token',
                            default=utils.env('OS_AUTH_TOKEN'),
                            help='Defaults to env[OS_AUTH_TOKEN].')

        parser.add_argument('--os-no-client-auth',
                            default=utils.env('OS_NO_CLIENT_AUTH'),
                            action='store_true',
                            help="Do not contact keystone for a token. "
                                 "Defaults to env[OS_NO_CLIENT_AUTH].")

        parser.add_argument('--sgs-url',
                            default=utils.env('SGS_URL'),
                            help='Defaults to env[SGS_URL].')

        parser.add_argument('--sgs-api-version',
                            default=utils.env('SGS_API_VERSION', default='1'),
                            help='Defaults to env[SGS_API_VERSION] or 1.')

        parser.add_argument('--os-service-type',
                            default=utils.env('OS_SERVICE_TYPE'),
                            help='Defaults to env[OS_SERVICE_TYPE].')

        parser.add_argument('--os-endpoint-type',
                            default=utils.env('OS_ENDPOINT_TYPE'),
                            help='Defaults to env[OS_ENDPOINT_TYPE].')

        parser.add_argument('--include-password',
                            default=bool(utils.env(
                                'SGS_INCLUDE_PASSWORD')),
                            action='store_true',
                            help='Send os-username and os-password to sgs.')

        self._append_global_identity_args(parser)

        return parser

    def get_subcommand_parser(self, version):
        parser = self.get_base_parser()

        self.subcommands = {}
        subparsers = parser.add_subparsers(metavar='<subcommand>')
        submodule = importutils.import_versioned_module(
            'sgsclient', version, 'shell'
        )
        self._find_actions(subparsers, submodule)
        self._find_actions(subparsers, self)

        self._add_bash_completion_subparser(subparsers)

        return parser

    def _add_bash_completion_subparser(self, subparsers):
        subparser = subparsers.add_parser(
            'bash_completion',
            add_help=False,
            formatter_class=HelpFormatter
        )
        self.subcommands['bash_completion'] = subparser
        subparser.set_defaults(func=self.do_bash_completion)

    def _find_actions(self, subparsers, actions_module):
        for attr in (a for a in dir(actions_module) if a.startswith('do_')):
            # I prefer to be hypen-separated instead of underscores.
            command = attr[3:].replace('_', '-')
            callback = getattr(actions_module, attr)
            desc = callback.__doc__ or ''
            help = desc.strip().split('\n')[0]
            arguments = getattr(callback, 'arguments', [])

            subparser = subparsers.add_parser(command, help=help,
                                              description=desc,
                                              add_help=False,
                                              formatter_class=HelpFormatter)
            subparser.add_argument('-h', '--help', action='help',
                                   help=argparse.SUPPRESS)
            self.subcommands[command] = subparser
            for (args, kwargs) in arguments:
                subparser.add_argument(*args, **kwargs)
            subparser.set_defaults(func=callback)

    def _discover_auth_versions(self, session, auth_url):
        # discover the API versions the server is supporting base on the
        # given URL
        v2_auth_url = None
        v3_auth_url = None
        try:
            ks_discover = discover.Discover(session=session, auth_url=auth_url)
            v2_auth_url = ks_discover.url_for('2.0')
            v3_auth_url = ks_discover.url_for('3.0')
        except ks_exc.ClientException as e:
            # Identity service may not support discover API version.
            # Lets trying to figure out the API version from the original URL.
            url_parts = urlparse.urlparse(auth_url)
            (scheme, netloc, path, params, query, fragment) = url_parts
            path = path.lower()
            if path.startswith('/v3'):
                v3_auth_url = auth_url
            elif path.startswith('/v2'):
                v2_auth_url = auth_url
            else:
                # not enough information to determine the auth version
                msg = ('Unable to determine the Keystone version '
                       'to authenticate with using the given '
                       'auth_url. Identity service may not support API '
                       'version discovery. Please provide a versioned '
                       'auth_url instead. error=%s') % (e)
                raise exc.CommandError(msg)

        return (v2_auth_url, v3_auth_url)

    def _get_keystone_auth(self, session, auth_url, **kwargs):
        auth_token = kwargs.pop('auth_token', None)
        if auth_token:
            return token.Token(
                auth_url,
                auth_token,
                project_id=kwargs.pop('project_id'),
                project_name=kwargs.pop('project_name'),
                project_domain_id=kwargs.pop('project_domain_id'),
                project_domain_name=kwargs.pop('project_domain_name'))

        # NOTE(starodubcevna): this is a workaround for the bug:
        # https://bugs.launchpad.net/python-openstackclient/+bug/1447704
        # Change that fix this error in keystoneclient was abandoned,
        # so we should use workaround until we move to keystoneauth.
        # The idea of the code came from glanceclient.

        (v2_auth_url, v3_auth_url) = self._discover_auth_versions(
            session=session,
            auth_url=auth_url)

        if v3_auth_url:
            # NOTE(starodubcevna): set user_domain_id and project_domain_id
            # to default as it done in other projects.
            return password.Password(auth_url,
                                     username=kwargs.pop('username'),
                                     user_id=kwargs.pop('user_id'),
                                     password=kwargs.pop('password'),
                                     user_domain_id=kwargs.pop(
                                         'user_domain_id') or 'default',
                                     user_domain_name=kwargs.pop(
                                         'user_domain_name'),
                                     project_id=kwargs.pop('project_id'),
                                     project_name=kwargs.pop('project_name'),
                                     project_domain_id=kwargs.pop(
                                         'project_domain_id') or 'default')
        elif v2_auth_url:
            return password.Password(auth_url,
                                     username=kwargs.pop('username'),
                                     user_id=kwargs.pop('user_id'),
                                     password=kwargs.pop('password'),
                                     project_id=kwargs.pop('project_id'),
                                     project_name=kwargs.pop('project_name'))
        else:
            # if we get here it means domain information is provided
            # (caller meant to use Keystone V3) but the auth url is
            # actually Keystone V2. Obviously we can't authenticate a V3
            # user using V2.
            exc.CommandError("Credential and auth_url mismatch. The given "
                             "auth_url is using Keystone V2 endpoint, which "
                             "may not able to handle Keystone V3 credentials. "
                             "Please provide a correct Keystone V3 auth_url.")

    def _setup_logging(self, debug):
        # Output the logs to command-line interface
        color_handler = handlers.ColorHandler(sys.stdout)
        logger_root = logging.getLogger(None).logger
        logger_root.level = logging.DEBUG if debug else logging.WARNING
        logger_root.addHandler(color_handler)

        # Set the logger level of special library
        logging.getLogger('iso8601') \
            .logger.setLevel(logging.WARNING)
        logging.getLogger('urllib3.connectionpool') \
            .logger.setLevel(logging.WARNING)

    def main(self, argv):
        # Parse args once to find version
        parser = self.get_base_parser()
        (options, args) = parser.parse_known_args(argv)
        self._setup_logging(options.debug)

        # build available subcommands based on version
        api_version = options.sgs_api_version
        subcommand_parser = self.get_subcommand_parser(api_version)
        self.parser = subcommand_parser

        # keystone_session = None
        # keystone_auth = None

        # Handle top-level --help/-h before attempting to parse
        # a command off the command line.
        if (not args and options.help) or not argv:
            self.do_help(options)
            return 0

        # Parse args again and call whatever callback was selected.
        args = subcommand_parser.parse_args(argv)

        # Short-circuit and deal with help command right away.
        if args.func == self.do_help:
            self.do_help(args)
            return 0
        elif args.func == self.do_bash_completion:
            self.do_bash_completion(args)
            return 0

        if not args.os_username and not args.os_auth_token:
            raise exc.CommandError("You must provide a username via"
                                   " either --os-username or env[OS_USERNAME]"
                                   " or a token via --os-auth-token or"
                                   " env[OS_AUTH_TOKEN]")

        if args.os_no_client_auth:
            if not args.sgs_url:
                raise exc.CommandError(
                    "If you specify --os-no-client-auth"
                    " you must also specify a SG-Service API URL"
                    " via either --sgs-url or env[SGS_URL]")

        else:
            # Tenant name or ID is needed to make keystoneclient retrieve a
            # service catalog, it's not required if os_no_client_auth is
            # specified, neither is the auth URL.
            if not any([args.os_tenant_name, args.os_tenant_id,
                        args.os_project_id, args.os_project_name]):
                raise exc.CommandError("You must provide a project name or"
                                       " project id via --os-project-name,"
                                       " --os-project-id, env[OS_PROJECT_ID]"
                                       " or env[OS_PROJECT_NAME]. You may"
                                       " use os-project and os-tenant"
                                       " interchangeably.")
            if not args.os_auth_url:
                raise exc.CommandError("You must provide an auth url via"
                                       " either --os-auth-url or via"
                                       " env[OS_AUTH_URL]")

        # TODO(luobin): use keystone get endpoint and kwargs
        kwargs = {}
        project_id = "468b0b3a8318403eba8e7d4fcf44e611"
        endpoint = "http://162.3.117.200:8975/v1/%s" % project_id

        client = sgs_client.Client(api_version, endpoint, **kwargs)

        args.func(client, args)

    def do_bash_completion(self, args):
        """Prints all of the commands and options to stdout."""
        commands = set()
        options = set()
        for sc_str, sc in self.subcommands.items():
            commands.add(sc_str)
            for option in list(sc._optionals._option_string_actions):
                options.add(option)

        commands.remove('bash-completion')
        commands.remove('bash_completion')
        print(' '.join(commands | options))

    @utils.arg('command', metavar='<subcommand>', nargs='?',
               help='Display help for <subcommand>')
    def do_help(self, args):
        """Display help about this program or one of its subcommands.

        """
        if getattr(args, 'command', None):
            if args.command in self.subcommands:
                self.subcommands[args.command].print_help()
            else:
                msg = "'%s' is not a valid subcommand"
                raise exc.CommandError(msg % args.command)
        else:
            self.parser.print_help()


class HelpFormatter(argparse.HelpFormatter):
    def start_section(self, heading):
        # Title-case the headings
        heading = '%s%s' % (heading[0].upper(), heading[1:])
        super(HelpFormatter, self).start_section(heading)


def main(args=sys.argv[1:]):
    try:
        SGServiceShell().main(args)

    except KeyboardInterrupt:
        print('... terminating sgs client', file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        if '--debug' in args or '-d' in args:
            raise
        else:
            print(encodeutils.safe_encode(six.text_type(e)), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
