from .compatibility import ast
from .errors import XunSyntaxError
from .util import assignment_target_shape
from .util import flatten_assignment_targets
from .util import indices_from_shape
from immutables import Map as frozenmap
import typing


class XunType:
    def __call__(self, *args, **kwargs):
        raise TypeError('XunType should not be used')

    def __repr__(self):
        return self.__class__.__name__
XunType = XunType()


class TerminalType:
    def __call__(self, *args, **kwargs):
        raise TypeError('TerminalType should not be used')

    def __getitem__(self, key):
        inst = self.__class__.__new__(self.__class__)
        inst.generic_type = key
        return inst

    def __repr__(self):
        r = self.__class__.__name__
        if hasattr(self, 'generic_type'):
            r += f'[{self.generic_type}]'
        return r
TerminalType = TerminalType()


"""
Types used:
* Xun
* Any
* Union
* TerminalType (can't be reused in comprehensions)
"""

def is_typing_tuple(t):
    # Python 3.6 operates with t.__origin__ is typing.Tuple, but for >3.6 it
    # is t.__origin__ is tuple
    return hasattr(t, '__origin__') and (
        t.__origin__ is tuple or t.__origin__ is typing.Tuple)


def type_is_xun_type(t):
    return t is XunType


def type_is_iterator(t):
    return t is typing.Iterator


class TypeDeducer:
    """
    Registers all variables and their types
    """
    def __init__(self, known_xun_functions):
        self.known_xun_functions = known_xun_functions
        self.with_names = []
        self.expr_name_type_map = frozenmap()

    def _replace(self, **kwargs):
        """
        Replace the existing values of class attributes with new ones.

        Parameters
        ----------
        kwargs : dict
            keyword arguments corresponding to one or more attributes whose
            values are to be modified

        Returns
        -------
        A new class instance with replaced attributes
        """
        attribs = {k: kwargs.pop(k, v) for k, v in vars(self).items()}
        if kwargs:
            raise ValueError(f'Got unexpected field names: {list(kwargs)!r}')
        inst = self.__class__.__new__(self.__class__)
        inst.__dict__.update(attribs)
        return inst

    def visit(self, node):
        member = 'visit_' + node.__class__.__name__
        visitor = getattr(self, member)
        return visitor(node)

    def visit_Assign(self, node):
        if len(node.targets) > 1:
            raise SyntaxError("Multiple targets not supported")

        target = node.targets[0]
        target_shape = assignment_target_shape(target)

        value_type = self.visit(node.value)

        if target_shape == (1,):
            self.with_names.append(target.id)
            with self.expr_name_type_map.mutate() as mm:
                mm[target.id] = value_type
                self.expr_name_type_map = mm.finish()
            return value_type

        indices = indices_from_shape(target_shape)
        flatten_targets = flatten_assignment_targets(target)

        with self.expr_name_type_map.mutate() as mm:
            for index, target in zip(indices, flatten_targets):
                self.with_names.append(target.id)
                target_type = value_type
                for i in index:
                    if is_typing_tuple(target_type):
                        target_type = target_type.__args__[i]
                mm.set(target.id, target_type)
            new_map = mm.finish()
        self.expr_name_type_map = new_map

        return value_type

    def visit_BoolOp(self, node):
        pass

    def visit_BinOp(self, node):
        left_type = self.visit(node.left)
        right_type = self.visit(node.right)
        if left_type is XunType or right_type is XunType:
            raise XunSyntaxError(
                'Cannot use xun function results as values in xun '
                'definition'
            )
        return typing.Any

    def visit_UnaryOp(self, node):
        pass

    def visit_IfExp(self, node):
        body_type = self.visit(node.body)
        orelse_type = self.visit(node.orelse)
        if body_type is not orelse_type:
            return TerminalType[typing.Union[body_type, orelse_type]]
        return body_type

    def visit_Dict(self, node: ast.Dict):
        """ We don't support xun keys
        Unknown type, because we can't reason about it (new issue)
        Hence, you can't forward it
        Weak coupling between keys and values
        """
        return TerminalType[typing.Dict]

    def visit_Set(self, node):
        """
        Since unordered, all types must be the same
        """
        set_type = self.visit(node.elts[0])
        for elt in node.elts:
            elt_type = self.visit(elt)
            if elt_type is not set_type:
                raise XunSyntaxError

        return set_type

    def visit_ListComp(self, node: ast.ListComp):
        """
        ListComps in Xun produces Tuples (because of posibility for different
        types) -> ListComp means Tuple Comprehension
        Not allowed to iterate over tuples
        Iterator of generators can only be a variable, or a
        Does not support ifs or is_async

        TODO
        my_iter -> Tuple[Any, Xun, Any, Xun]
        [i for i in my_iter] -> Tuple[Any, Xun, Any, Xun]
        """
        # Register the local variables in each generator
        with self.expr_name_type_map.mutate() as local_scope:
            for generator in node.generators:
                iter_types = self.visit(generator.iter)
                # TODO Raise TypeError if TerminalType is used

                target = generator.target
                target_shape = assignment_target_shape(target)

                if target_shape == (1,):
                    local_scope.set(target.id, iter_types)
                    continue

                indices = indices_from_shape(target_shape)
                flatten_targets = flatten_assignment_targets(target)

                for index, target in zip(indices, flatten_targets):
                    target_type = iter_types
                    if is_typing_tuple(target_type):
                        for i in index:
                            target_type = target_type.__args__[i]
                    local_scope.set(target.id, target_type)

            # Add mapping over known local variables while visiting the element
            # of the comprehension
            return self._replace(expr_name_type_map=local_scope.finish()
                ).visit(node.elt)


    def visit_SetComp(self, node):
        # Ex: {i for i in range(10)}
        # Terminal Type, can't be reused in comprehension
        # This must be of one type
        # Get something that must have same type
        pass

    def visit_DictComp(self, node):
        # Ex: {k: v for k, v in ...}
        # Terminal Type, can't be reused in comprehension
        # This must be of one type
        pass

    def visit_GeneratorExp(self, node):
        # Ex: (i for i in range(10))
        # This works
        # Terminal type or this becomes a list
        return typing.Iterator

    def visit_Await(self, node):
        self.raise_class_not_allowed(node)

    def visit_Yield(self, node):
        self.raise_class_not_allowed(node)

    def visit_YieldFrom(self, node):
        self.raise_class_not_allowed(node)

    def visit_Compare(self, node):
        self.raise_class_not_allowed(node)

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Name) and
            node.func.id in self.known_xun_functions):
            return XunType
        return typing.Any

    def visit_FormattedValue(self, node):
        pass

    def visit_JoinedStr(self, node):
        pass

    def visit_Constant(self, node):
        return typing.Any

    def visit_Attribute(self, node):
        pass

    def visit_Subscript(self, node):
        pass

    def visit_Starred(self, node):
        pass

    def visit_Name(self, node: ast.Name):
        if node.id in self.expr_name_type_map.keys():
            return self.expr_name_type_map[node.id]
        if node.id in self.local_expr_name_type_map.keys():
            return self.local_expr_name_type_map[node.id]
        return typing.Any

    def visit_List(self, node):
        return self.visit_Tuple(node)

    def visit_Tuple(self, node):
        return typing.Tuple[tuple(self.visit(elt) for elt in node.elts)]

    def visit_Slice(self, node):
        pass

    #
    # For 3.6 compatibility
    #

    def visit_Num(self, node):
        return typing.Any

    def visit_Str(self, node):
        return typing.Any

    def visit_Bytes(self, node):
        return typing.Any

    def visit_NameConstant(self, node):
        return typing.Any

    def visit_Ellipsis(self, node):
        return typing.Any

    def raise_class_not_allowed(self, node):
        raise XunSyntaxError(f'{node.__class__} not allowed in xun definitions')