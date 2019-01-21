'''
Module with utilities for checking / parsing / setting input arguments from the cli
(download, process).

:date: Feb 27, 2018

.. moduleauthor:: Riccardo Zaccarelli <rizac@gfz-potsdam.de>
'''
import os
import sys
import re
from datetime import datetime, timedelta

from future.utils import string_types

from stream2segment.utils.resources import yaml_load, get_ttable_fpath, \
    get_templates_fpath, normalizedpath
from stream2segment.utils import get_session, strptime, load_source
from stream2segment.traveltimes.ttloader import TTTable
from stream2segment.io.db.models import Fdsnws
from stream2segment.download.utils import Authorizer


class BadArgument(Exception):
    '''An exception whose string method is similar to click formatted output. It
    supports sub-classes for most common argument errors
    '''
    def __init__(self, param_name, error, msg_preamble=''):
        '''init method

        The formatted output, depending on the truthy value of the arguments will be:

        "%(msg_preamble) %(param_name): %(error)"
        "%(param_name): %(error)"
        "%(msg_preamble) %(param_name)"
        "%(param_name)"

        :param param_name: the parameter name (string)
        :param error: the original exception, or a string message
        :param msg_preamble: the optional message preamble, as string
        '''
        super(BadArgument, self).__init__(str(error))
        self.msg_preamble = msg_preamble
        self.param_name = str(param_name) if param_name else None

    @property
    def message(self):
        msg = '%s' if not self.msg_preamble else self.msg_preamble.strip() + " %s"
        err_msg = self.args[0]  # in ValueError, is the error_msg passed in the constructor
        pname = ('"%s"' % self.param_name) if self.param_name else \
            'unknown parameter (check input arguments)'
        ret = (msg % pname) + (": " + err_msg if err_msg else '')
        return ret[0:1].upper() + ret[1:]

    def __str__(self):
        ''''''
        return "Error: %s" % self.message


class MissingArg(BadArgument):

    def __init__(self, param_name):
        '''A BadArgument notifying a missing value of some argument'''
        super(MissingArg, self).__init__(param_name, '', "Missing value for")


class BadValueArg(BadArgument):

    def __init__(self, param_name, error):
        '''A BadArgument notifying a bad value of some argument'''
        super(BadValueArg, self).__init__(param_name, error, "Invalid value for")


class BadTypeArg(BadArgument):

    def __init__(self, param_name, error):
        '''A BadArgument notifying a bad type of some argument'''
        super(BadTypeArg, self).__init__(param_name, error, "Invalid type for")


class ConflictingArgs(BadArgument):
    '''A BadArgument notifying conflicting argument names'''

    def __init__(self, *param_names):
        '''A BadArgument notifying conflicting argument names'''
        # little hack: build a string wiothout first and last quote (will be added in super-class)
        param_name = self.formatnames(*param_names)
        super(ConflictingArgs, self).__init__(param_name, '', "Conflicting names")

    @staticmethod
    def formatnames(*param_names):
        # little hack: build a string wiothout first and last quote (will be added in super-class)
        return " / ".join('"%s"' % p for p in param_names)[1:-1]


class UnknownArg(BadArgument):

    def __init__(self, param_name):
        '''A BadArgument notifying an unknown argument'''
        super(UnknownArg, self).__init__(param_name, '', "no such option")


def parse_arguments(yaml_dic, *params):
    '''Parses yaml_dic parameters according to `params`. Modifies in-place `yaml_dic` and
    returns the set of `yaml_dic` parameters not parsed.

    :param params: a list of dicts. Each dict defines how to parse the given parameter and can
        have the keys and values:
        'names': (mandatory) list / tuple of the parameter name(s): the first parameter name
            found in `yaml_dic` will be used, and  a :class:`ConflictingArgs` exception is raised
            if any of the other names is also found in `yaml_dic` keys. It can be also a string,
            in which case the function behaves as if `names` was a list with that string as only
            element
        'defvalue': (optional) when provided, and no parameter name is found in `yaml_dic`,
            this is used as value. If not provided and no name in `names` is in
            in `yaml_dic`, a :class:`MissingArg` exception is raised
        'newname': (optional) string denoting the new parameter name which will replace the
            old one. When missing, it defaults to `names[0]`
        'newvalue': (optional) a callable which accepts the parameter value as argument and
            returns a new value. The function can safely raise: its exception(s) will be converted
            to :class:BadTypeArg or :class:BadValueArg depending on the cause

        For each element of `params`, this function parses the given argument and raises the
        appropriate `BadArgument` exceptions
    :raise: BadArgument

    :return: the set of names of `yaml_dic` not parsed

    '''
    remainingkeys = set(yaml_dic)
    for param in params:
        names = param['names']
        if not isinstance(names, (list, tuple)):
            names = (names,)
        name, value = get(yaml_dic, names, param.get('defvalue', None))
        # name is the actual key in yaml_dict:
        newvalue = parse(name, value, param.get('newvalue', lambda val: val))
        # names[0] is the key that will be set on yaml_dct, if newname is missing:
        newname = param.get('newname', names[0])
        # if the newname is not names[0], remove name (not names[0]) from yanl_dic:
        if newname != name:
            yaml_dic.pop(name, None)
        # set new name and new (parsed) value:
        yaml_dic[newname] = newvalue
        # remove the parsed keys from remainingkeys:
        remainingkeys -= set(names)
    return remainingkeys


def get(dic, names, default_ifmissing=None):
    '''Similar to `dic.get`, gets the first `names` element `n` found in `dic`, and returns
    the tuple `(n, dic[n])`.

    Raises :class:`MissingArg` if no name is found, and :class:`ConflictingArgs` if more than
    one key is found

    :param dic: the source dict
    :param names: list/tuple of `dic` keys to be searched.. It can be also a string,
        in which case the function behaves as if `names` was a list with that string as only
        element
    :param default_if_missing: if provided and not None (the default), then this is the
        value returned if no name is found. If not provided, and no name is found in
        `dic`, :class:`MissingArg` is raised
    '''
    # note: use self._names to keep declaration order of this argument name(s)
    try:
        if not isinstance(names, (list, tuple)):
            names = (names,)
        keys_in = [par for par in names if par in dic]
        if len(keys_in) > 1:
            raise ConflictingArgs(*keys_in)
        elif not keys_in:
            if default_ifmissing is not None:
                return names[0], default_ifmissing
            raise KeyError()
        name = keys_in[0]
        return name, dic[name]

    except KeyError as _:
        raise MissingArg(ConflictingArgs.formatnames(*names))


def parse(name, value, parsefunc, *args, **kwargs):
    '''Calls `parsefunc` on the given value, and returns the result.
    Raises :class:`BadArgument` exceptions wrapping ValueError and TypeErrors, if raised

    :param name: the name of the mapped to value value (str), e.g., the parameter name
        whose value needs to be parsed.
        It is used in the message of the exception raised, if any
    :param value: any python object.
    :param parsefunc: the function to be called with `value` as first argument
    :param args: optional positional arguments to be passed to `parsefunc`
    :param args: optional keyword arguments to be passed to `parsefunc`
    '''
    try:
        return parsefunc(value, *args, **kwargs)
    except TypeError as terr:
        raise BadTypeArg(name, terr)
    except Exception as exc:
        raise BadValueArg(name, exc)


def typesmatch(value, *other_values):
    '''checks that value is of the same type (same class, or subclass) of *any* `other_value`
    (at least one). Raises TypeError if that's not the case

    :param value: a python object
    :param other_values: python objects. This function raises if value is NOT of the same type of
        any other_values types

    :return: value
    '''
    for other_value in other_values:
        if issubclass(value.__class__, other_value.__class__):
            return value
    raise TypeError("%s expected, found %s" % (" or ".join(str(type(_)) for _ in other_values),
                                               str(type(value))))


def nslc_param_value_aslist(value):
    '''Returns a nslc (network/station/location/channel) parameter value converted as list.
    This method cleans-up and checks `value` splitting each of its string elements
    with the comma "," and aggregating all the string chunks into a single list, after performing
    some sanity check. The resulting list is also sorted alphabetically
    (for unit testing and readibility).
    Raises ValueError in case some sanity checks fail (e.g., conflicts, syntax errors)

    Examples:

    nslc_param_value_aslist
    arguments (any means:
    any value in [0,1,2,3])   Result (with comment)
    ========================= =================================================================
    (['A','D','C','B'])  ['A', 'B', 'C', 'D']  # note result is sorted
    ('B,C,D,A')          ['A', 'B', 'C', 'D']  # same as above
    ('A*, B??, C*')      ['A*', 'B??', 'C*']  # fdsn wildcards accepted
    ('!A*, B??, C*')     ['!A*', 'B??', 'C*']  # we support negations: !A* means "not A*"
    (' A, B ')           ['A', 'B']  # leading and trailing spaces ignored
    ('*')                []  # if any chunk is '*', then [] (=match all) is returned
    ([])                 []  # same as above
    ('  ')               ['']  # this means: match the empty string (strip the string)
    ("")                 [""]  # same as above
    ("!")                ['!']  # match any non empty string
    ("!*")               this raises (you cannot specify "discard all")
    ("!H*, H*")          this raises (it's a paradox)
    (" A B,  CD")        this raises ('A B' invalid: only leading and trailing spaces allowed)

    :param value: string or iterable of strings: (iterable in this context means python iterable
        EXCEPT strings). If string, the argument will be converted
        to the list [value] to make it iterable before processing it
    '''
    try:
        strings = set()

        # we assume, when parsearg is not list, that parsearg is str in both python2 and python3,
        # i.e. it is NOT bytes in python2. The line below checks if is an iterable first:
        # in python2, it is sufficient to say it's not a string
        # in python3, we need to check that is no str also
        if not hasattr(value, "__iter__") or isinstance(value, str):
            # it's an iterable not a string
            value = [value]

        for string in value:
            splitted = string.split(",")
            for chunk in splitted:
                chunk = chunk.strip()
                if ' ' in chunk:
                    raise Exception("invalid space char(s): '%s'" % chunk)
                # if i == 3 (location) convert '--' to '':
                strings.add(chunk)

        # some checks:
        if "!*" in strings:  # discard everything is not valid
            raise ValueError("'!*' (=discard all) invalid")
        elif "*" in strings:  # accept everything or X => X is redundant
            strings = set(_ for _ in strings if _[0:1] == '!')
        else:
            for string in strings:  # accept A end discard A is not valid
                opposite = "!%s" % string
                if opposite in strings:
                    raise Exception("conflicting values: '%s' and '%s'" % (string, opposite))

        return sorted(strings)

    except Exception as exc:
        raise ValueError(str(exc))


def extract_dburl_if_yamlpath(value, param_name='dburl'):
    """
    Returns the database path from 'value':
    'value' can be a file (in that case is assumed to be a yaml file with the
    `param_name` key in it, which must denote a db path) or the database path otherwise
    """
    if not isinstance(value, string_types) or not value:
        raise TypeError('please specify a string denoting either a path to a yaml file with the '
                        '`dburl` parameter defined, or a valid db path')
    return yaml_load(value)[param_name] if (os.path.isfile(value)) else value


def keyval_list_to_dict(value):
    """parses optional event query args (when the 'd' command is issued) into a dict"""
    # use iter to make a dict from a list whose even indices = keys, odd ones = values
    # https://stackoverflow.com/questions/4576115/convert-a-list-to-a-dictionary-in-python
    itr = iter(value)
    return dict(zip(itr, itr))


def create_session(dburl):
    '''Creates an asql-alchemy session from dburl. Raises TypeError if dburl os not
    a string, or any SqlAlchemy exception if the session could not be created

    :param dburl: string denoting a database url (currently postgres and sqlite supported
    '''
    if not isinstance(dburl, string_types):
        raise TypeError('string required, %s found' % str(type(dburl)))
    return get_session(dburl, scoped=False)


def create_auth(restricted_data, dataws, configfile=None):
    '''Creates an Auth class (handling authentication/authorization)
    from the given restricted_data

    :param restricted_data: either file path, to token, token data in bytes, or
        tuple (user, password). If None, or the empty string, None is returned
    '''
    if restricted_data in ('', None, b''):
        restricted_data = None
    elif isinstance(restricted_data, string_types) and configfile is not None:
        restricted_data = normalizedpath(restricted_data, configfile)
    ret = Authorizer(restricted_data)
    # here we have 4 cases: two ok ('eida' + token, any other fdsn + username & password)
    # Bad cases: eida + username & password: raise
    # any other fdsn + token: return normally, we might have provided a single eida datacenter
    #    in which case the parameter set is fine.
    if dataws.lower() == 'eida' and ret.userpass:
        raise ValueError('downloading from EIDA requires a token, not username and password')
    return ret


def parse_inventory(inventory):
    '''parses inventory returning True, False or 'only'
    '''
    inv = inventory
    if isinstance(inventory, string_types):
        if inventory.lower() == 'true':
            inventory = True
        elif inventory.lower() == 'false':
            inventory = False
        else:
            inventory = inventory.lower()
    if inventory not in (True, False, 'only'):
        raise ValueError('value can be true, false or only, %s provided' % str(inv))
    return inventory


def load_tt_table(file_or_name):
    '''Loads the given TTTable object from the given file path or name. If name (string)
    it must match any of the builtin TTTable .npz files defined in this package
    Raises TypeError or any Exception that TTTable might raise (including when the file is not
    found)
    '''
    if not isinstance(file_or_name, string_types):
        raise TypeError('string required, not %s' % str(type(file_or_name)))
    filepath = get_ttable_fpath(file_or_name)
    if not os.path.isfile(filepath):
        filepath = file_or_name
    if not os.path.isfile(filepath):
        raise Exception('file or builtin model name not found')
    return TTTable(filepath)


def valid_date(obj):
    try:
        return strptime(obj)  # if obj is datetime, returns obj
    except (TypeError, ValueError) as _:
        try:
            days = int(obj)
            now = datetime.utcnow()
            endt = datetime(now.year, now.month, now.day, 0, 0, 0, 0)
            return endt - timedelta(days=days)
        except Exception:
            pass
        if isinstance(_, TypeError):
            raise TypeError(("iso-formatted datetime string, datetime "
                             "object or int required, found %s") % str(type(obj)))
        else:
            raise _


def valid_fdsn(url):
    '''Returns url if it matches a FDSN service (valid strings are 'eida' and 'iris'),
    raises ValueError or TypeError otherwise'''
    if not isinstance(url, string_types):
        raise TypeError('string required')
    if url.lower() in ('eida', 'iris'):
        return url
    return Fdsnws(url).url()


def load_config_for_download(config, parseargs, **param_overrides):
    '''loads download arguments from the given config (yaml file or dict) after parsing and
    checking some of the dict keys.

    :return: a dict loaded from the given `config` and with parseed arguments (dict keys)

    Raises BadArgument in case of parsing errors, missisng arguments, conflicts etcetera
    '''
    try:
        dic = yaml_load(config, **param_overrides)
    except Exception as exc:
        raise BadValueArg('config', exc)

    # normalize eventws_query_args: the sub-dict is correctly updated. The function
    # yaml_load updates nested sub-dict values, so that if both dic['eventws_query_args']
    # and param_overrides['eventws_query_args'] contain, e.g. the key 'minlat', the key
    # is overridden in dic['eventws_query_args']. But
    # param_overrides['eventws_query_args'] might contain 'minlatitude' instead of 'minlat'
    # which should override 'minlat' in dic['eventws_query_args'] as well.
    # Check these cases of double names:
    overrides_eventdic = param_overrides.get('eventws_query_args', {})
    yaml_eventdic = dic['eventws_query_args']
    for par in overrides_eventdic:
        for find, rep in (('latitude', 'lat'), ('longitude', 'lon'), ('magnitude', 'mag')):
            twinpar = par.replace(find, rep)
            if twinpar == par:
                twinpar.replace(rep, find)
            if twinpar != par and twinpar in yaml_eventdic:
                # rename the overridden par with the previously set config par:
                yaml_eventdic[twinpar] = yaml_eventdic.pop(par)
                break

    if parseargs:
        # few variables:
        configfile = config if (isinstance(config, string_types) and os.path.isfile(config))\
            else None

        params = [
            {
             'names': ['minlatitude', 'minlat'],
             'newvalue': lambda val: return [-90.0 90.0]
            },
            {
             'names': ['maxlatitude', 'maxlat'],
             'newvalue': lambda val: return [-90.0 90.0]
            },
            {
             'names': ['minlongitude', 'minlon'],
             'newvalue': lambda val: return [-180.0 180.0]
            },
            {
             'names': ['maxlongitude', 'maxlon'],
             'newvalue': lambda val: return [-180.0 180.0]
            },
            {
             'names': ['minmagnitude', 'minmag'],
             'newvalue': lambda val: return [-180.0 180.0]
            },
            {
             'names': ['maxmagnitude', 'maxmag'],
             'newvalue': lambda val: return [-180.0 180.0]
            },
            {
             'names': ['inventory'],
             'newvalue': parse_inventory
             },
            {
             'names': ['restricted_data'],
             'newname': 'authorizer',
             'newvalue': lambda val: create_auth(val, dic['dataws'], configfile)
            },
            {
             'names': ['dburl'],
             'newname': 'session',
             'newvalue': create_session
            },
            {
             'names': ['traveltimes_model'],
             'newname': 'tt_table',
             'newvalue': load_tt_table
            },
            {
             'names': ('start', 'starttime'),
             'newvalue': valid_date
            },
            {
             'names': ('end', 'endtime'),
             'newvalue': valid_date
            },
            {
             'names': ['eventws'],
             'newvalue': valid_fdsn
            },
            {
             'names': ['dataws'],
             'newvalue': valid_fdsn
            },
            {
             'names': ('networks', 'net', 'network'),
             'defvalue': [],
             'newvalue': nslc_param_value_aslist
            },
            {
             'names': ('stations', 'sta', 'station'),
             'defvalue': [],
             'newvalue': nslc_param_value_aslist
            },
            {
             'names': ('locations', 'loc', 'location'),
             'defvalue': [],
             'newvalue': nslc_param_value_aslist
            },
            {
             'names': ('channels', 'cha', 'channel'),
             'defvalue': [],
             'newvalue': nslc_param_value_aslist
            },
            ]

        remainingkeys = parse_arguments(dic, *params)

        # For all remaining arguments, just check the type as it should match the
        # default download config shipped with this package:
        orig_config = yaml_load(get_templates_fpath("download.yaml"))
        for key in remainingkeys:
            try:
                other_value = orig_config[key]
            except KeyError:
                raise UnknownArg(key)
            parse(key, dic[key], typesmatch, other_value)

    return dic


def load_pyfunc(pyfile, funcname):
    '''Returns the python module from the given python file'''
    if not isinstance(pyfile, string_types):
        raise TypeError('string required, not %s' % str(type(pyfile)))

    if not os.path.isfile(pyfile):
        raise Exception('file does not exist')

    pymoduledict = load_source(pyfile).__dict__
    if funcname not in pymoduledict:
        raise Exception('function "%s" not found in %s' % (str(funcname), pyfile))
    return pymoduledict[funcname]


def get_funcname(funcname=None):
    '''Returns the python module from the given python file'''
    if funcname is None:
        funcname = default_processing_funcname()

    if not isinstance(funcname, string_types):
        raise TypeError('string required, not %s' % str(type(funcname)))

    return funcname


def default_processing_funcname():
    '''returns 'main', the default function name for processing, when such a name is not given'''
    return 'main'


def filewritable(filepath):
    '''checks that the file is writable, i.e. that is a string and its directory exists'''
    if not isinstance(filepath, string_types):
        raise TypeError('string required, found %s' % str(type(filepath)))

    if not os.path.isdir(os.path.dirname(filepath)):
        raise ValueError('cannot write file: parent directory does not exist')

    return filepath


def load_config_for_process(dburl, pyfile, funcname=None, config=None, outfile=None,
                            **param_overrides):
    '''checks process arguments.
    Returns the tuple session, pyfunc, config_dict,
    where session is the dql alchemy session from `dburl`,
    pyfunc is the python function loaded from `pyfile`, and config_dict is the dict loaded from
    `config` which must denote a path to a yaml file, or None (config_dict will be empty
    in this latter case)
    '''
    session = parse('dburl', dburl, create_session)
    funcname = parse('funcname', funcname, get_funcname)
    try:
        # yaml_load accepts a file name or a dict
        config = yaml_load({} if config is None else config, **param_overrides)
    except Exception as exc:
        raise BadValueArg('config', exc)

    # NOTE: contrarily to the download routine, we cannot check the types of the config because
    # no parameter is mandatory, and thus they might NOT be present in the config.

    pyfunc = parse('pyfile', pyfile, load_pyfunc, funcname)
    if outfile is not None:
        parse('outfile', outfile, filewritable)
    # nothing more to process
    return session, pyfunc, funcname, config


def load_session_for_dinfo(dburl):
    return parse('dburl', dburl, create_session)
