from __future__ import unicode_literals

from argparse import ArgumentParser, SUPPRESS as SUPPRESS_ARG
import errno
import logging.config
import os.path
from os import stat
import re
import sys

from six import string_types
import yaml

from . import __version__
from .acl import Acl
from .utils import (
    deepget,
    deepset,
    UserError,
)
from .role import RoleOptions


logger = logging.getLogger(__name__)


class MultilineFormatter(logging.Formatter):
    def format(self, record):
        s = super(MultilineFormatter, self).format(record)
        if '\n' not in s:
            return s

        lines = s.splitlines()
        d = record.__dict__.copy()
        for i, line in enumerate(lines[1:]):
            record.message = line
            lines[1+i] = self._fmt % record.__dict__
        record.__dict__ = d

        return '\n'.join(lines)


class ColoredStreamHandler(logging.StreamHandler):

    _color_map = {
        logging.DEBUG: '37',
        logging.INFO: '1;39',
        logging.WARN: '96',
        logging.ERROR: '91',
        logging.CRITICAL: '1;91',
    }

    def format(self, record):
        lines = super(ColoredStreamHandler, self).format(record)
        color = self._color_map.get(record.levelno, '39')
        lines = ''.join([
            '\033[0;%sm%s\033[0m' % (color, line)
            for line in lines.splitlines(True)
        ])
        return lines


def acldict(value):
    if not hasattr(value, 'items'):
        raise ValueError('acl_dict must be a dict')

    return {
        k: Acl(k, **v)
        for k, v in value.items()
    }


def raw(v):
    return v


def ldapquery(value):
    query = dict(Configuration.DEFAULTS['ldap']['default_query'], **value)

    if 'attribute' in query:
        query['attributes'] = query['attribute']
        del query['attribute']
    if isinstance(query['attributes'], string_types):
        query['attributes'] = [query['attributes']]

    return query


def rolerule(value):
    rule = value

    if isinstance(rule, string_types):
        rule = dict(names=[rule])

    if 'name' in rule:
        rule['names'] = rule.pop('name')
    if 'names' in rule and isinstance(rule['names'], string_types):
        rule['names'] = [rule['names']]

    if 'parent' in rule:
        rule['parents'] = rule.pop('parent')
    rule.setdefault('parents', [])
    if isinstance(rule['parents'], string_types):
        rule['parents'] = [rule['parents']]

    options = rule.setdefault('options', {})

    if isinstance(options, string_types):
        options = options.split()

    if isinstance(options, list):
        options = {
            o[2:] if o.startswith('NO') else o: not o.startswith('NO')
            for o in options
        }

    rule['options'] = RoleOptions(**options)
    return rule


def grantrule(value):
    if not isinstance(value, dict):
        raise ValueError('Grant rule must be a dict.')
    if 'acl' not in value:
        raise ValueError('Missing acl to grant rule.')

    allowed_keys = set([
        'acl', 'database', 'schema', 'role_match', 'role_attribute',
    ])
    defined_keys = set(value.keys())

    if defined_keys - allowed_keys:
        msg = 'Unknown parameter to grant rules: %s' % (
            ', '.join(allowed_keys - defined_keys)
        )
        raise ValueError(msg)

    return value


def ismapping(value):
    # Check whether a YAML value is supposed to be a single mapping.
    if not isinstance(value, dict):
        return False
    return bool({'grant', 'ldap', 'role', 'roles'} >= set(value.keys()))


def mapping(value):
    # A single mapping from a query to a set of role rules. This function
    # translate random YAML to cannonical schema.

    if not isinstance(value, dict):
        raise ValueError("Mapping should be a dict.")

    if 'ldap' in value:
        value['ldap'] = ldapquery(value['ldap'])

    if 'role' in value:
        value['roles'] = value['role']
    if 'roles' not in value:
        value['roles'] = []
    if isinstance(value['roles'], string_types + (dict,)):
        value['roles'] = [value['roles']]

    value['roles'] = [rolerule(r) for r in value['roles']]

    if 'grant' in value:
        if isinstance(value['grant'], dict):
            value['grant'] = [value['grant']]
        value['grant'] = [grantrule(g) for g in value['grant']]

    if not value['roles'] and 'grant' not in value:
        # Don't accept unused LDAP queries.
        raise ValueError("Missing role or grant rule.")

    return value


def syncmap(value):
    # Validate and translate raw YAML value to cannonical form used internally.
    #
    # A sync map has the following canonical schema:
    #
    # <__common__|dbname>:
    #   <__common__|schema>:
    #   - ldap: <ldapquery>
    #     roles:
    #     - <rolerule>
    #     - ...
    #   ...
    # ...
    #
    # But we accept a wide variety of shorthand schemas:
    #
    # Single mapping:
    #
    # roles: [<rolerule>]
    #
    # List of mapping:
    #
    # - roles: [<rolerule>]
    # - ...
    #
    # dict of dbname->single mapping
    #
    # appdb:
    #   roles: <rolerule>
    #
    # dict of dbname-> list of mapping
    #
    # appdb:
    # - roles: <rolerule>
    #
    # dict of dbname->schema->single mapping
    #
    # appdb:
    # - roles: <rolerule>
    # dict of dbname->schema->single mapping
    #
    # appdb:
    #   appschema:
    #     roles: <rolerule>

    if not value:
        raise ValueError("Empty mapping.")

    if isinstance(value, list):
        value = dict(__common__=value)

    if ismapping(value):
        value = dict(__common__=[value])

    if not isinstance(value, dict):
        raise ValueError("Illegal value for sync_map.")

    for dbname, ivalue in value.items():
        if isinstance(ivalue, list):
            value[dbname] = ivalue = dict(__common__=ivalue)

        if ismapping(ivalue):
            value[dbname] = ivalue = dict(__common__=[ivalue])

        for schema, maplist in ivalue.items():
            if isinstance(maplist, dict):
                ivalue[schema] = maplist = [maplist]

            maplist[:] = [mapping(m) for m in maplist]

    return value


def define_arguments(parser):
    parser.add_argument(
        '-c', '--config',
        action='store', dest='config', metavar='PATH',
        help='path to YAML configuration file (env: LDAP2PG_CONFIG)'
    )
    parser.add_argument(
        '-n', '--dry',
        action='store_true', dest='dry',
        help="don't touch Postgres, just print what to do (env: DRY)"
    )
    parser.add_argument(
        '-N', '--real',
        action='store_false', dest='dry',
        help="real mode, apply changes to Postgres (env: DRY)"
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true', dest='verbose',
        help="add debug messages including SQL and LDAP queries (env: VERBOSE)"
    )
    parser.add_argument(
        '--color',
        action='store_true', dest='color',
        help="force color output (env: COLOR)"
    )
    parser.add_argument(
        '--no-color',
        action='store_false', dest='color',
        help="force plain text output (env: COLOR)"
    )
    parser.add_argument(
        '-?', '--help',
        action='help',
        help='show this help message and exit')
    parser.add_argument(
        '-V', '--version',
        action='version',
        help='show version and exit',
        version=__package__ + ' ' + __version__,
    )


class Mapping(object):
    """Fetch value from either file or env var."""

    _auto_env = object()

    def __init__(self, path, env=_auto_env, secret=False, processor=raw):
        self.path = path
        self.arg = path.replace(':', '_')

        env = env or []
        if env == self._auto_env:
            env = self.arg.upper()
        self.env = env
        if isinstance(self.env, string_types):
            self.env = [self.env]

        self.processor = processor
        if isinstance(secret, string_types):
            secret = re.compile(secret)
        self.secret = secret

    def process_env(self, environ):
        # Get value from env var
        for env in self.env:
            try:
                value = environ[env]
                logger.debug("Read %s from %s.", self.path, env)
                break
            except KeyError:
                continue
        else:
            raise KeyError()

        return value

    def process_file(self, file_config):
        # Get value from parsed YAML file.
        unsecured_file = file_config.get('world_readable', True)

        value = deepget(file_config, self.path)

        # Check whether this value is secret.
        if hasattr(self.secret, 'search'):
            secret = self.secret.search(value)
        else:
            secret = self.secret

        if secret and unsecured_file:
            raise ValueError("Refuse to load secret from world readable file.")

        logger.debug("Read %s from YAML.", self.path)
        return value

    def process_arg(self, args):
        # Get value from argparse result.
        value = getattr(args, self.arg)
        logger.debug("Read %s from argv.", self.path)
        return value

    def process(self, default, file_config={}, environ={}, args=object()):
        # This is the sources of configuration, ordered by priority desc. If a
        # process_* function raises KeyError or AttributeError, it is ignored.
        sources = [
            (self.process_arg, args),
            (self.process_env, environ),
            (self.process_file, file_config),
        ]

        for source in sources:
            callable_, args = source[0], source[1:]
            try:
                value = callable_(*args)
                break
            except (AttributeError, KeyError):
                continue
        else:
            value = default

        return self.processor(value)


class ConfigurationError(UserError):
    def __init__(self, message):
        super(ConfigurationError, self).__init__(
            message, exit_code=os.EX_CONFIG,
        )


class NoConfigurationError(Exception):
    pass


class Configuration(dict):
    DEFAULTS = {
        'dry': True,
        'verbose': False,
        'color': False,
        'ldap': {
            'host': '',
            'port': 389,
            'bind': None,
            'password': None,
            'default_query': {
                'base': '',
                'filter': '(objectClass=organizationalRole)',
                'attributes': ['cn'],
            },
        },
        'postgres': {
            'dsn': '',
            'blacklist': ['pg_*', 'postgres'],
        },
        'acl_dict': {},
        'sync_map': [],
    }

    MAPPINGS = [
        Mapping('color'),
        Mapping('dry'),
        Mapping('verbose', env=['VERBOSE', 'DEBUG']),
        Mapping('ldap:host'),
        Mapping('ldap:port'),
        Mapping('ldap:bind'),
        Mapping('ldap:password', secret=True),
        Mapping(
            'postgres:dsn', env='PGDSN',
            secret=r'(?:password=|:[^/][^/].*@)',
        ),
        Mapping('postgres:blacklist', env=None),
        Mapping('acl_dict', processor=acldict),
        Mapping('sync_map', env=None, processor=syncmap)
    ]

    def __init__(self):
        super(Configuration, self).__init__(self.DEFAULTS)

    _file_candidates = [
        './ldap2pg.yml',
        '~/.config/lda2pg.yml',
        '/etc/ldap2pg.yml',
    ]

    def find_filename(self, environ=os.environ, args=None):
        custom = getattr(args, 'config', environ.get('LDAP2PG_CONFIG'))

        if '-' == custom:
            return custom, 0o400
        elif custom:
            candidates = [custom]
        else:
            candidates = self._file_candidates

        for candidate in candidates:
            candidate = os.path.expanduser(candidate)
            try:
                logger.debug("Trying %s.", candidate)
                stat_ = stat(candidate)
                return os.path.realpath(candidate), stat_.st_mode
            except OSError as e:
                if e.errno == errno.EACCES:
                    logger.warn("Can't read %s: permission denied.", candidate)

        if custom:
            message = "Can't access configuration file %s." % (custom,)
            raise UserError(message, exit_code=os.EX_NOINPUT)
        else:
            raise NoConfigurationError("No configuration file found")

    EPILOG = """\

    ldap2pg requires a configuration file to describe LDAP queries and
    role mappings. See project home for further details.

    By default, ldap2pg runs in dry mode.
    """.replace(4 * ' ', '')

    def load(self, argv=None):
        # argv processing.
        logger.debug("Processing CLI arguments.")
        parser = ArgumentParser(
            add_help=False,
            # Only store value from argv. Defaults are managed by
            # Configuration.
            argument_default=SUPPRESS_ARG,
            description="Swiss-army knife to sync Postgres ACL from LDAP.",
            epilog=self.EPILOG,
        )
        define_arguments(parser)
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)

        if hasattr(args, 'verbose') or hasattr(args, 'color'):
            # Switch to verbose before loading file.
            self['verbose'] = getattr(args, 'verbose', self['verbose'])
            self['color'] = getattr(args, 'color', self['color'])
            logging.config.dictConfig(self.logging_dict())

        logger.info("Starting ldap2pg %s.", __version__)

        # File loading.
        try:
            filename, mode = self.find_filename(os.environ, args)
        except NoConfigurationError:
            logger.debug("No configuration file found.")
            file_config = {}
        else:
            if filename == '-':
                logger.info("Reading configuration from stdin.")
                file_config = self.read(sys.stdin, mode)
            else:
                logger.info("Using %s.", filename)
                try:
                    with open(filename) as fo:
                        file_config = self.read(fo, mode)
                except OSError as e:
                    msg = "Failed to read configuration: %s" % (e,)
                    raise UserError(msg)

        # Now merge all config sources.
        try:
            self.merge(file_config=file_config, environ=os.environ, args=args)
        except ValueError as e:
            raise ConfigurationError("Failed to load configuration: %s" % (e,))

        logger.debug("Configuration loaded.")

    def merge(self, file_config, environ=os.environ, args=object()):
        for mapping in self.MAPPINGS:
            value = mapping.process(
                default=deepget(self, mapping.path),
                file_config=file_config,
                environ=environ,
                args=args,
            )
            deepset(self, mapping.path, value)

    def read(self, fo, mode):
        payload = yaml.load(fo) or {}
        if isinstance(payload, list):
            payload = dict(sync_map=payload)
        if not isinstance(payload, dict):
            raise ConfigurationError("Configuration file must be a mapping.")
        payload['world_readable'] = bool(mode & 0o077)
        return payload

    def logging_dict(self):
        formatter = 'verbose' if self['verbose'] else 'info'

        return {
            'version': 1,
            'formatters': {
                'info': {
                    '()': __name__ + '.MultilineFormatter',
                    'format': '%(message)s',
                },
                'verbose': {
                    '()': __name__ + '.MultilineFormatter',
                    'format': '[%(name)-16s %(levelname)8s] %(message)s',
                },
            },
            'handlers': {
                'raw': {
                    '()': 'logging.StreamHandler',
                    'formatter': formatter,
                },
                'colored': {
                    '()': __name__ + '.ColoredStreamHandler',
                    'formatter': formatter,
                },
            },
            'root': {
                'level': 'WARNING',
                'handlers': ['colored' if self['color'] else 'raw'],
            },
            'loggers': {
                __package__: {
                    'level': 'DEBUG' if self['verbose'] else 'INFO',
                },
            },
        }
