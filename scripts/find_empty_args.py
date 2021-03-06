import argparse

# noinspection PyUnresolvedReferences
from _context import vidlu
from vidlu.utils import tree
import vidlu.utils.func as vuf

# example: python find_empty_args.py vidlu.learning.models.ResNet18

parser = argparse.ArgumentParser()
parser.add_argument('namespace', type=str)
parser.add_argument('func', type=str)
args = parser.parse_args()

namespace_str, func_str = args.namespace, args.func

exec(f"from {namespace_str} import *")

try:
    func = eval(func_str)
    empty_args = vuf.find_params_deep(func, lambda _, v: v is vuf.Empty)
    n = len(empty_args)
    print(f"Found {n} empty argument{'' if n == 1 else 's'} in {func_str}{'.' if n == 0 else ':'}")
    for ea in empty_args:
        print(' ', '/'.join(ea))
    print("As tree:")
    print(' ', tree.unflatten([(x, '?') for x in empty_args], vuf.ArgTree))
except (NameError, AttributeError):
    print(f"{namespace_str} contains the following:")
    namespace = eval(namespace_str)
    for obj_name in dir(namespace):
        obj = getattr(namespace, obj_name)
        if (not obj_name.startswith('_') and callable(obj)
                and (not hasattr(obj, '__module__') or obj.__module__ == namespace_str)):
            print(f"  {obj_name}")
    raise
