'''
Module implementing the functionalities that allow issuing sql select statements from
config files, command line or via GUI input controls
via string expression on database tables columns

:date: Mar 6, 2017

.. moduleauthor:: Riccardo Zaccarelli <rizac@gfz-potsdam.de>
'''

from datetime import datetime
import shlex

# iterating over dictionary keys with the same set-like behaviour on Py2.7 as on Py3
from future.utils import viewitems

import numpy as np
from sqlalchemy import asc, and_, desc, inspect
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.orm.collections import InstrumentedList

# from sqlalchemy.exc import InvalidRequestError


def exprquery(sa_query, conditions, orderby=None, distinct=None):
    """
    Enhance the given sql-alchemy query `sa_query` with conditions
    and ordering given in form of (string) expression, returning a new sql alchemy query.
    Columns (and relationships, if any) are extracted from the string keys of `conditions`
    by detecting the reference model class from `sa_query` first column
    (`sa_query.column_descriptions[0]`): thus **pay attention to the argument order of sa_query**.
    Consqeuently, joins are automatically added inside this method, if needed (if a join is
    already present, in `sa_query` and should be required by any of `conditions`, it won't be
    added twice)
    The returned query is a valid sql-alchemy query and can be further manipulated
    **in most cases**: a case when it's not possible is when issuing a `group_by` in `postgres`
    (for info, see
    http://stackoverflow.com/questions/18061285/postgresql-must-appear-in-the-group-by-clause-or-be-used-in-an-aggregate-functi).
    In these cases a normal SqlAlchemy query must be issued

    Example:
    ```
    # pseudo code:

    Parent:
        id = Column(primary_key=True,...)
        child_id: foreign_key(Child.id)
        age = Column(Integer, ...)
        birth = Column(DateTime, ...)
        children = relationship(Child,...)

    Child:
        id = Column(primary_key=True,...)
        age = Column(Integer, ...)
        birth = Column(DateTime, ...)
        parent = relationship(Parent,...)

    sess = ...  # sql-alchemy session
    sa_query = sess.query  # sql-alchemy session's query object

    # return all parents who have children:
    exprquery(sa_query(Parent), {'children', 'any'})

    # return all parents id's who have children:
    exprquery(sa_query(Parent.id), {'children', 'any'})

    # return all parents who have adult children:
    exprquery(sa_query(Parent), {'children.age', '>=18'})

    # return all parents born before 1980 who have adult children:
    exprquery(sa_query(Parent), {'birth': '<1980-01-01', 'children.age', '>=18'})

    # return all parents who have adult children, sorted (ascending) by parent's age (2 solutions):
    exprquery(sa_query(Parent), {'children.age', '>=18'}, ['age'])  # or
    exprquery(sa_query(Parent), {'children.age', '>=18'}, [('age', 'asc')])

    # return all parents who have adult children, sorted (ascending) by parent's age and then
    # descending by parent's id:
    exprquery(sa_query(Parent), {'children.age', '>=18'}, [('age', 'asc'), ('id', 'desc')])

    # Finally, note that these three are equivalent and valid:
    date1980 = datetime(1980, 1, 1)
    exprquery(sa_query(Parent).filter(Parent.birth < date1980), {'children.age', '>=18'})
    exprquery(sa_query(Parent), {'children.age', '>=18'}).filter(Parent.birth < date1980)
    exprquery(sa_query(Parent), {'birth': '<1980-01-01', 'children.age', '>=18'})
    ```

    :param sa_query: any sql-alchemy query object
    :param conditions: a dict of string columns mapped to **strings** expression, e.g.
    "column2": "[1, 45]" or "column1": "true" (note: string, not the boolean True)
    A string column is an expression denoting an attribute of the reference model class
    and can include relationships.
    Example: if the reference model tablename is 'mymodel', then a string column 'name'
    will refer to 'mymodel.name', 'name.id' denotes on the other hand a relationship 'name'
    on 'mymodel' and will refer to the 'id' attribute of the table mapped by 'mymodel.name'.
    The values of the dict on the other hand are string expressions in the form recognized
    by `binexpr`. E.g. '>=5', '["4", "5"]' ...
    For each condition mapped to a falsy value (e.g., None or empty string), the condition is
    discarded. See note [*] below for auto-added joins from columns.
    :param orderby: a list of string columns (same format
    as `conditions` keys), or a list of tuples where the first element is
    a string column, and the second is either "asc" (ascending) or "desc" (descending). In the
    first case, the order is "asc" by default. See note [*] below for auto-added joins from
    orderby columns.

    :return: a new sel-alchemy query including the given conditions and ordering

    [*] Note on auto-added joins: if any given `condition` or `orderby` key refers to
    relationships defined on the reference model class, then necessary joins are appended to
    `sa_query`, *unless already present* (this should also avoid the
    warning 'SAWarning: Pathed join target', currently in `sqlalchemy.orm.query.py:2105`).
    """
    # get the table model from the query's FIRST column description
    model = sa_query.column_descriptions[0]['entity']
    parsed_conditions = []
    joins = set()  # relationships have an hash, this assures no duplicates
    # set already joined tables. We use the private method _join_entities although it's not
    # documented anywhere (we inspected via eclipse debug to find the method):
    already_joined_models = set(_.class_ for _ in sa_query._join_entities)
    # if its'an InstrumentedAttribute, use the class
    relations = inspect(model).relationships

    if conditions:
        for attname, expression in viewitems(conditions):
            if not expression:  # discard falsy expressions (empty strings, None's)
                # note that expressions MUST be strings
                continue
            relationship, column = _get_rel_and_column(model, attname, relations)
            if relationship is not None and \
                    get_rel_refmodel(relationship) not in already_joined_models:
                joins.add(relationship)
            condition = binexpr(column, expression)
            parsed_conditions.append(condition)

    directions = {"asc": asc, "desc": desc}
    orders = []
    if orderby:
        for order in orderby:
            try:
                column_str, direction = order
            except ValueError:
                column_str, direction = order, "asc"
            directionfunc = directions[direction]
            relationship, column = _get_rel_and_column(model, column_str, relations)
            if relationship is not None and \
                    get_rel_refmodel(relationship) not in already_joined_models:
                joins.add(relationship)
            # FIXME: we might also write column.asc() or column.desc()
            orders.append(directionfunc(column))

    if joins:
        sa_query = sa_query.join(*joins)
    if parsed_conditions:
        sa_query = sa_query.filter(and_(*parsed_conditions))
    if orders:
        sa_query = sa_query.order_by(*orders)
    if distinct is True:
        sa_query = sa_query.distinct()
    return sa_query


def _get_rel_and_column(model, colname, relations=None):
    if relations is None:
        relations = inspect(model).relationships  # ['station'].remote_side
    cols = colname.split(".")
    obj = model
    rel = None
    for col in cols:
        tmp = getattr(obj, col)
        if col in relations and obj is model:
            rel = tmp
            obj = get_rel_refmodel(relations[col])
        else:
            obj = tmp
    return rel, obj


def get_rel_refmodel(relationship):
    '''returns the relationship's reference table model
    :param relationship: the InstrumentedAttribute retlative to a relationship. Example. Given
        a model `model`, and a relationship e.g. `r_name=inspect(model).relationships.keys()[i],
        then `relationship=getattr(model, r_name)`'''
    return relationship.mapper.class_


def binexpr(column, expr):
    """Returns an :class:`sqlalchemy.sql.expression.BinaryExpression` to be used as `query.filter`
    argument from the given column and the given expression. Supports the operators given in
    :function:`stream2segment.io.db.sqlevalexpr.split` and the types given in `parsevals`:
    (`int`s, `float`s, `datetime`s, `bool`s and `str`s)
    :param column: an sqlkalchemy model column
    :param expr: a string expression (see `split`)

    :example:
    ```
    # given a model with column `column1`
    binexpr(model.column1, '>=5')
    ```
    """
    operator, values = split(expr)
    values = parsevals_sql(column, values)
    if operator == '=':
        return column == values[0] if len(values) == 1 else column.in_(values)
    elif operator == "!=":
        return column != values[0] if len(values) == 1 else ~column.in_(values)
    elif operator == ">":
        return and_(*[column > val for val in values])
    elif operator == "<":
        return and_(*[column < val for val in values])
    elif operator == ">=":
        return and_(*[column >= val for val in values])
    elif operator == "<=":
        return and_(*[column <= val for val in values])
    else:
        cond = column.between(values[0], values[1])
        if operator == 'open':
            cond = cond & (column != values[0]) & (column != values[1])
        elif operator == 'leftopen':
            cond = cond & (column != values[0])
        elif operator == 'rightopen':
            cond = cond & (column != values[1])
        elif operator != 'closed':
            raise ValueError("Invalid operator %s" % operator)
        return cond
    raise ValueError("Invalid expression %s" % expr)


def split(expr):
    """
        Splits the expression into its operator(s) and its value.

        :param: expression: a string which is first stripped (i.e., leading and trailing spaces
        are omitted) and then either:
        1. starts with (zero or more spaces and):
            "<", "=", "==", "!=", ">", "<=", ">="
        2. starts with "[", "(", "]" **and** ends with "]" , "[", ")", where "[", "]" denote the
        closed interval (endpoints included) and the other symbols an open interval (endpoints
        excluded)

        :return: the operator (one of the symbol above) and the remaining string. Note that the
        operator is normalized to "=" in case 1 if either "=" or "==", and in case 2 is "open",
        "leftopen", "rightopen", "closed"
    """
    expr = expr.strip()
    if expr[:2] in ("<=", ">=", "==", "!="):
        return '=' if expr[:2] == '==' else expr[:2], expr[2:].strip()
    elif expr[0] in ("<", ">", "="):
        return expr[0], expr[1:].strip()
    elif expr[0] in ("(", "]", "["):
        assert expr[-1] in (")", "[", "]")
        newexpr = expr[1:-1].replace(",", " ")
        assert len(shlex.split(newexpr)) == 2
        if expr[0] == '[':
            val = "closed" if expr[-1] == ']' else "rightopen"
        else:
            val = "leftopen" if expr[-1] == ']' else "open"
        return val, newexpr
    else:
        return "=", expr


def parsevals_sql(column, expr_value):
    """
        parses `expr_value` according to the model column type. Supports `int`s, `float`s,
        `datetime`s, `bool`s and `str`s.
        :param expr_value: a value given as command line argument(s). Thus, quoted strings will
        be recognized removing the quotation symbols.
        The list of values will then be casted to the python type of the given column.
        Note that the values are intended to be
        in SQL syntax, thus NULL or null for python None's. Datetime's must be input in ISO format
        (with or without spaces)

        :Example:
        ```
        # given a model with int column 'column1'
        parsevals(model.column1, '4 null 5 6') = [4, None, 5, 6]
        ```
    """
    try:
        return parsevals(get_pytype(get_sqltype(column)), expr_value)
    except ValueError as verr:
        raise ValueError("column %s: %s" % (str(column), str(verr)))


def parsevals(pythontype, expr_value):
    """
        parses `expr_value` according to the given python type. Supports `int`s, `float`s,
        `datetime`s, `bool`s and `str`s.
        :param expr_value: if bool, int, float, None or datetime, or iterable of those values,
        a value given as command line argument(s). Thus, quoted strings will
        be recognized removing the quotation symbols.
        The list of values will then be casted to the python type of the given column.
        Note that the values are intended to be
        in SQL syntax, thus NULL or null for python None's. Datetime's must be input in ISO format
        (with or without spaces)

        :Example:
        ```
        # given a model with int column 'column1'
        parsevals(int, '4 null 5 6') = [4, None, 5, 6]
        ```
    """
    _NONES = ("null", "NULL")
    vals = shlex.split(expr_value)
    if pythontype == float:
        return [None if x in _NONES else float(x) for x in vals]
    elif pythontype == int:
        return [None if x in _NONES else int(x) for x in vals]
    elif pythontype == bool:
        # bool requires a user defined function for parsing javascript/python strings (see below)
        return [None if x in _NONES else _bool(x) for x in vals]
    elif pythontype == datetime:
        return np.array(vals, dtype="datetime64[us]").tolist()  # works with None's
    elif pythontype == str:
        return [None if x in _NONES else str(x) for x in vals]

    raise ValueError('Unsupported python type %s' % pythontype)


def _bool(val):
    '''parses javascript booleans true false and returns a python boolean'''
    if val in ('false', 'False', 'FALSE'):
        return False
    elif val in ('true', 'True', 'TRUE'):
        return True
    return bool(val)


def get_sqltype(obj):
    '''Returns the sql type associated with `obj`.

    :param obj: an object with an 'expression' method, e.g. sqlalchemy.sql.schema.Column or
        :class:`sqlalchemy.orm.attributes.QueryableAttribute` (for instance
         :class:`sqlalchemy.orm.attributes.InstrumentedAttribute`, i.e. the model's attributes
        mapping db columns)

    :return: An object defined in :class:`sqlalchemy.sql.sqltypes`, e.g. `Integer`
        the method  :function:`get_pytype` of the returned object defines the relative python type
    '''
    try:
        return obj.expression.type
    except NotImplementedError:
        return None


def get_pytype(sqltype):
    """Returns the python type associated to the given sqltype.
    :param sqltype: an object as returned by `get_sqltype`
    :return: a python type class asscoaiated to `sqltype`, or None
    """
    try:
        return sqltype.python_type
    except NotImplementedError:
        return None


def inspect_model(model, exclude=None):
    '''Returns a list of tuples:
    ```
        (att_name, python_type)
    ```
    of the given model. `python_type` is the type of the attribute `att_name`, it can be None if
    the relative method raises.
    Attributes referring to db table foreign keys will not be returned.
    Attributes referring to db relationships R will inspect the relationship's mapped table T,
    returning a list of attributes names in the format:
    '<R name>.<T attribute name>'

    :param exclude: a list of strings or :class:`sqlalchemy.orm.attributes.InstrumentedAttribute`s
    (e.g., attributes mapping db table columns or model relationships)
    to exclude from the returned iterator

    '''
    data = inspect_list(model, 'rcp', False, True, exclude)
    # Note: python types (function get_sqltype) of relationships return 'boolean' as python type.
    # We need to get them and replace with object
    rels = set(inspect(model).relationships.keys())
    for d in data:
        if d[0] in rels:
            d[1] = object
        else:
            try:
                d[1] = get_pytype(get_sqltype(d[1]))
            except Exception as exc:
                d[1] = None
    return data


def inspect_instance(instance, exclude=None):
    '''Returns a list of tuples:
    ```
        (att_name, att_val)
    ```
    of the given instance. `att_name` denotes an attribute of `instance`.
    Attributes referring to db table foreign keys will not be returned.
    Attributes referring to db relationships R will inspect the relationship's mapped table T,
    returning a list of attributes names in the format:
    '<R name>.<T attribute name>'

    :param exclude: a list of strings or :class:`sqlalchemy.orm.attributes.InstrumentedAttribute`s
        (e.g., attributes mapping db table columns or model relationships)
        to exclude from the returned iterator, in addition to Foreign keys
    '''
    return inspect_list(instance, 'rcp', False, True, exclude)


def inspect_list(model, type_='rcp', fkeys=True, deep_relationships=False, exclude=None):
    '''Returns a list of two-element lists:
    ```
    att_name, att_val.
    ```
    from the given `model` argument, where:
    - `att_name` is a string denoting an attribute of `model`. If 'deep_relationships=True',
        Attributes referring to db relationships R will inspect the relationship's mapped table T,
        returning a list of attributes names in the format:
        '<R name>.<T attribute name>'
    - `att_val` is the attribute value, if `model` defines a mapped db table *instance*, or
    the :class:`sqlalchemy.orm.attributes.InstrumentedAttribute`, if `model` defines a
    mapped table *class*

    the order of the returned tuples will be:
    - attributes denoting primary key columns first
    - all other attributes instance of `QueryableAttribute` (attributes denoting table columns,
        hybrid properties, etc...) in alphabetical ascending order
    - all relationships in alphabetical ascending order. If `deep_relationships` = True, each
      relation attribute is in turn sorted according to the primary key and all
      `QueryableAttribute`s in alphabetical ascending order

    :param model: an ORM model or an ORM model instance. If the former, the attribute values
        will be :class:`sqlalchemy.orm.attributes.InstrumentedAttribute`(s), otherwise the instance
        values
    :param type_: string denoting what to return. Is any combination of 'r', 'p' and 'c'.
        'c': return attributes mapped to db columns
        'r': return attributes mapped to sql-alchemy relationships
        'p': return attributes which are any other queryable property on the model
    :param fkeys: boolean (default: True), whether to return columns that are foreign keys to
        some other table
    :param deep_relationships: boolean (default: True) whether to inspect all relationships found,
        running this method on the mapped class (if `model` is a model class) or instance
        (if `model` is a model instance)
    :param exclude: a list of strings or :class:`sqlalchemy.orm.attributes.InstrumentedAttribute`s
        (e.g., attributes mapping db table columns or model relationships)
        to exclude from the returned iterator

    :return: a list of tuples of `model` attributes mapped to their values
    '''
    if not model:
        return []

    if not exclude:
        exclude = set([])  # faster 'in' search operator
    elif not isinstance(exclude, set):
        exclude = set(exclude)

    try:
        mapper = inspect(model)
    except NoInspectionAvailable:
        return []

    if mapper is model:  # this happens when the object has anything to inspect, eg.
        # an AppenderQuery object resulting from some relationship configured in some particular way
        return []

    instance = model
    inspecting_instance = False
    if mapper.mapper.class_ == model.__class__:
        inspecting_instance = True
        model = instance.__class__
        mapper = inspect(model)

    ret_pkeys = []
    ret_atts = []
    ret_rels = []

    def append(attname, excluded_, list_):
        class_attval = getattr(model, attname)
        if class_attval not in excluded_:
            list_.append([attname,
                          getattr(instance, attname) if inspecting_instance else class_attval])
            return True
        return False

    if 'c' in type_:
        columns = mapper.columns
        fk_cols_excluded = \
            set() if fkeys else set(f.parent for f in mapper.mapped_table.foreign_keys)
        pkey_columns = set(mapper.mapped_table.primary_key.columns)
        if 'c' in type_:
            for attname, column in columns.items():
                if attname in exclude or column in fk_cols_excluded:
                    continue
                append(attname, exclude, ret_pkeys if column in pkey_columns else ret_atts)

    if 'r' in type_ or 'p' in type_:
        rels = mapper.relationships
        rel_names = rels.keys()
        if 'p' in type_:
            excluded_attrs2 = exclude | set(rel_names) | set(columns.keys())
            for attname in dir(model):
                if attname in excluded_attrs2 or attname[:2] == '__':
                    continue
                class_attval = getattr(model, attname)
                if not isinstance(class_attval, QueryableAttribute):
                    continue
                append(attname, excluded_attrs2, ret_atts)

        if 'r' in type_:
            if not deep_relationships:
                for attname in rel_names:
                    if attname not in exclude:
                        append(attname, exclude, ret_rels)
            else:
                # relation names will be prepended with the class name + dot.
                for attname in rel_names:
                    if attname not in exclude:
                        class_attval = getattr(model, attname)
                        if class_attval not in exclude:
                            sub_model = getattr(instance, attname) if inspecting_instance else \
                                get_rel_refmodel(class_attval)
                            type_2 = type_.replace('r', '')
                            if isinstance(sub_model, InstrumentedList):
                                relname2index = {}
                                for subm in sub_model:
                                    for a, v in inspect_list(subm, type_2, fkeys, False, exclude):
                                        rel_attname = ("%s.%s" % (attname, a))
                                        index = relname2index.get(rel_attname, None)
                                        if index is None:
                                            index = len(ret_rels)
                                            relname2index[rel_attname] = index
                                            ret_rels.append([rel_attname, []])
                                        ret_rels[index][1].append(v)
                            else:
                                for a, v in inspect_list(sub_model, type_2, fkeys, False, exclude):
                                    rel_attname = ("%s.%s" % (attname, a))
                                    ret_rels.append([rel_attname, v])

    ret_pkeys.sort(key=lambda item: item[0])
    ret_atts.sort(key=lambda item: item[0])
    return ret_pkeys + ret_atts + ret_rels
