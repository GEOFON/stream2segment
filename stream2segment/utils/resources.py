'''
Module for easily accessing all project specific resources.

:date: Feb 20, 2017

.. moduleauthor:: Riccardo Zaccarelli <rizac@gfz-potsdam.de>
'''
from os import listdir
from os.path import join, dirname, abspath, normpath, isfile, isabs, splitext
import re
from collections import defaultdict

# python2-3 compatibility for items and viewitems:
from future.utils import viewitems, viewkeys, string_types
import yaml


def _get_main_path():
    """Returns the main root path of the project 'stream2segment', the **parent** folder of the
    `_get_package_path()`"""
    # we are in ./stream2segment/utils.resources.py, so we need to get up 3 times
    return normpath(abspath(dirname(dirname(dirname(__file__)))))


def _get_package_path():
    """Returns the main root path of the package 'stream2segment', the **child** folder of the
    `_get_main_path()`"""
    # we are in ./stream2segment/utils.resources.py, so we need to get up 3 times
    return join(_get_main_path(), "stream2segment")


def get_resources_fpath(filename):
    """Returns the resource file with given filename inside the package `resource` folder
    :param filename: a filename relative to the resource folder
    """
    resfolder = join(_get_package_path(), "resources")
    return join(resfolder, filename)


def get_templates_dirpath():
    """Returns the templates directory path (located inside the package `resource` folder)
    """
    return get_resources_fpath("templates")


def get_traveltimes_dirpath():
    """Returns the travel time table directory path (located inside the package `resource` folder)
    """
    return get_resources_fpath("traveltimes")


def get_templates_fpaths(*filenames):
    """Returns the template file paths with given filename(s) inside the package `templates` of the
    `resource` folder. If filenames is empty (no arguments), returns all files (no dir) in the
    `templates` folder
    :param filenames: a list of file names relative to the templates folder. With no argument,
    returns all valid files inside that directory
    """
    templates_path = get_templates_dirpath()
    if not filenames:
        filenames = listdir(templates_path)

    return list(join(templates_path, _name) for _name in filenames)


def get_templates_fpath(filename):
    """Returns the template file path with given filename inside the package `templates` of the
    `resource` folder
    :param filename: a filename relative to the templates folder
    """
    return get_templates_fpaths(filename)[0]


def version(onerr=""):
    """Returns the program version saved in the main root dir 'version' file.
    :param onerr (string, "" when missing): what to return in case of IOError.
    If 'raise', then the exception is raised
    """
    try:
        with open(join(_get_main_path(), "version")) as _:
            return _.read().strip()
    except IOError as exc:
        if onerr == 'raise':
            raise exc
        return onerr


def get_ws_fpath():
    """Returns the web-service config file (yaml)"""
    return get_resources_fpath(filename='ws.yaml')


def yaml_load(filepath, **updates):
    """Loads a yaml file into a dict (if `filepath` is a `dict`, skips loading). Then:
    1. If `filepath` denotes a file path (and not a dict),
       normalizes non-absolute sqlite path values relative to `filepath`, if any
    2. updates the dict values with `updqtes` and returns the yaml dict. The update is
       recursive, meaning that nested dict values will be updated recursively and not completely
       overridden

    :param filepath: string or dict. If string, it must denote a path to an existing .yaml file
    :param updates: arguments which will updates the yaml dict before it is returned
    """
    if isinstance(filepath, string_types):
        with open(filepath, 'r') as stream:
            ret = yaml.safe_load(stream)
    elif isinstance(filepath, dict):
        ret = filepath
    elif hasattr(filepath, 'read'):
        ret = yaml.safe_load(filepath)
    else:
        raise TypeError('required file path (string), file object or dict, '
                        '%s found' % str(type(filepath)))

    # update recursively (which means subdicts are updated as well and not overridden):
    def update(dic1, dic2):
        '''update dic1 with dic2 recursively'''
        dickeys = {k: dic2.pop(k) for k in viewkeys(dic1) if isinstance(dic1[k], dict) and
                   isinstance(dic2.get(k, None), dict)}
        dic1.update(dic2)
        for k in dickeys:
            update(dic1[k], dickeys[k])

    update(ret, updates)

    if isinstance(filepath, string_types):
        # convert sqlite into absolute paths, if any. This does not convert nested sub-dict strings
        configfilepath = abspath(dirname(filepath))
        # convert relative sqlite path to absolute, assuming they are relative to the config:
        sqlite_prefix = 'sqlite:///'
        # we cannot modify a dict while in iteration, thus create a new dict of possibly
        # modified sqlite paths and use later dict.update
        newdict = {}
        for key, val in viewitems(ret):
            try:
                if val.startswith(sqlite_prefix) and ":memory:" not in val:
                    dbpath = val[len(sqlite_prefix):]
                    npath = normalizedpath(dbpath, configfilepath)
                    if npath != dbpath:
                        newdict[key] = sqlite_prefix + npath
            except AttributeError:
                pass

        ret.update(newdict)
    return ret


def normalizedpath(path, basedir):
    '''normalizes `path` is it's not absolute, making it relative to `basedir`.
    If path is already absolute, returns it as it is

    :param path: the path
    :param basedir: the base directory path
    '''
    if isabs(path):
        return path
    return abspath(normpath(join(basedir, path)))


def yaml_load_doc(filepath, varname=None, preserve_newlines=False):
    """Loads the doc from a yaml. The doc is intended to be all *consecutive* commented lines
    (with *no* leading spaces) before each top-level variable (nested variables are not considered).
    If `varname` is None (the default), the returned dict is a defaultdict which returns as
    string values (**unicode** strings in python 2) or an empty string for non-found documented
    variables.
    If `varname` is not None, as soon as the doc for `varname` is found, this function
    returns that doc string, and not the whole dict, or the empty string if nothing is found
    :param filepath: The yaml file to read the doc from
    :param varname: if None, returns a `defaultdict` with all docs (consecutive
    commented lines before) the yaml top-level variables. Otherwise, return the doc for the
    given variable name (string)
    :param preserve_newlines: boolean. Whether to preserve newlines in comment
    or not. If False (the default), each variable comment is returned as a single line,
    concatenating parsed lines with a space
    """
    comments = []
    reg_yaml_var = re.compile("^([^:]+):.*")
    reg_comment = re.compile("^#+(.*)")
    ret = defaultdict(str) if varname is None else ''
    isbytes = None
    with open(filepath, 'r') as stream:
        while True:
            line = stream.readline()  # last char of line is a newline
            if isbytes is None:
                isbytes = isinstance(line, bytes)
            if not line:
                break
            m = reg_comment.match(line)
            if m and m.groups():  # set comment
                # note that our group does not include last newline
                comments.append(m.groups()[0].strip())  # del leading and trailing spaces, if any
            else:  # try to see if it's a variable, and in case set the doc (if any)
                if comments:  # parse variable only if we have comments
                    # otherwise each nested variable is added to the dict with empty comment
                    m = reg_yaml_var.match(line)
                    if m and m.groups():
                        var_name = m.groups()[0]
                        comment = ("\n" if preserve_newlines else " ").join(comments)
                        docstring = comment.decode('utf8') if isbytes else comment
                        if varname is None:
                            ret[var_name] = docstring
                        elif varname == var_name:
                            ret = docstring
                            break
                # in any case, if not comment, reset comments:
                comments = []
    return ret


def get_ttable_fpath(basename):
    '''Returns the file for the given traveltimes table
    :param basename: the file name (with or without extension) located under
    `get_traveltimestables_dirpath()`
    '''
    if not splitext(basename)[1]:
        basename += ".npz"
    return join(get_traveltimes_dirpath(), basename)
