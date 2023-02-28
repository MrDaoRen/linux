# SPDX-License-Identifier: BSD-3-Clause

import collections
import importlib
import os
import traceback
import yaml


# To be loaded dynamically as needed
jsonschema = None


class SpecElement:
    """Netlink spec element.

    Abstract element of the Netlink spec. Implements the dictionary interface
    for access to the raw spec. Supports iterative resolution of dependencies
    across elements and class inheritance levels. The elements of the spec
    may refer to each other, and although loops should be very rare, having
    to maintain correct ordering of instantiation is painful, so the resolve()
    method should be used to perform parts of init which require access to
    other parts of the spec.

    Attributes:
        yaml        raw spec as loaded from the spec file
        family      back reference to the full family

        name        name of the entity as listed in the spec (optional)
        ident_name  name which can be safely used as identifier in code (optional)
    """
    def __init__(self, family, yaml):
        self.yaml = yaml
        self.family = family

        if 'name' in self.yaml:
            self.name = self.yaml['name']
            self.ident_name = self.name.replace('-', '_')

        self._super_resolved = False
        family.add_unresolved(self)

    def __getitem__(self, key):
        return self.yaml[key]

    def __contains__(self, key):
        return key in self.yaml

    def get(self, key, default=None):
        return self.yaml.get(key, default)

    def resolve_up(self, up):
        if not self._super_resolved:
            up.resolve()
            self._super_resolved = True

    def resolve(self):
        pass


class SpecAttr(SpecElement):
    """ Single Netlink atttribute type

    Represents a single attribute type within an attr space.

    Attributes:
        value      numerical ID when serialized
        attr_set   Attribute Set containing this attr
    """
    def __init__(self, family, attr_set, yaml, value):
        super().__init__(family, yaml)

        self.value = value
        self.attr_set = attr_set
        self.is_multi = yaml.get('multi-attr', False)


class SpecAttrSet(SpecElement):
    """ Netlink Attribute Set class.

    Represents a ID space of attributes within Netlink.

    Note that unlike other elements, which expose contents of the raw spec
    via the dictionary interface Attribute Set exposes attributes by name.

    Attributes:
        attrs      ordered dict of all attributes (indexed by name)
        attrs_by_val  ordered dict of all attributes (indexed by value)
        subset_of  parent set if this is a subset, otherwise None
    """
    def __init__(self, family, yaml):
        super().__init__(family, yaml)

        self.subset_of = self.yaml.get('subset-of', None)

        self.attrs = collections.OrderedDict()
        self.attrs_by_val = collections.OrderedDict()

        val = 0
        for elem in self.yaml['attributes']:
            if 'value' in elem:
                val = elem['value']

            attr = self.new_attr(elem, val)
            self.attrs[attr.name] = attr
            self.attrs_by_val[attr.value] = attr
            val += 1

    def new_attr(self, elem, value):
        return SpecAttr(self.family, self, elem, value)

    def __getitem__(self, key):
        return self.attrs[key]

    def __contains__(self, key):
        return key in self.attrs

    def __iter__(self):
        yield from self.attrs

    def items(self):
        return self.attrs.items()


class SpecOperation(SpecElement):
    """Netlink Operation

    Information about a single Netlink operation.

    Attributes:
        value       numerical ID when serialized, None if req/rsp values differ

        req_value   numerical ID when serialized, user -> kernel
        rsp_value   numerical ID when serialized, user <- kernel
        is_call     bool, whether the operation is a call
        is_async    bool, whether the operation is a notification
        is_resv     bool, whether the operation does not exist (it's just a reserved ID)
        attr_set    attribute set name

        yaml        raw spec as loaded from the spec file
    """
    def __init__(self, family, yaml, req_value, rsp_value):
        super().__init__(family, yaml)

        self.value = req_value if req_value == rsp_value else None
        self.req_value = req_value
        self.rsp_value = rsp_value

        self.is_call = 'do' in yaml or 'dump' in yaml
        self.is_async = 'notify' in yaml or 'event' in yaml
        self.is_resv = not self.is_async and not self.is_call

        # Added by resolve:
        self.attr_set = None
        delattr(self, "attr_set")

    def resolve(self):
        self.resolve_up(super())

        if 'attribute-set' in self.yaml:
            attr_set_name = self.yaml['attribute-set']
        elif 'notify' in self.yaml:
            msg = self.family.msgs[self.yaml['notify']]
            attr_set_name = msg['attribute-set']
        elif self.is_resv:
            attr_set_name = ''
        else:
            raise Exception(f"Can't resolve attribute set for op '{self.name}'")
        if attr_set_name:
            self.attr_set = self.family.attr_sets[attr_set_name]


class SpecFamily(SpecElement):
    """ Netlink Family Spec class.

    Netlink family information loaded from a spec (e.g. in YAML).
    Takes care of unfolding implicit information which can be skipped
    in the spec itself for brevity.

    The class can be used like a dictionary to access the raw spec
    elements but that's usually a bad idea.

    Attributes:
        proto     protocol type (e.g. genetlink)

        attr_sets  dict of attribute sets
        msgs       dict of all messages (index by name)
        msgs_by_value  dict of all messages (indexed by name)
        ops        dict of all valid requests / responses
    """
    def __init__(self, spec_path, schema_path=None):
        with open(spec_path, "r") as stream:
            spec = yaml.safe_load(stream)

        self._resolution_list = []

        super().__init__(self, spec)

        self.proto = self.yaml.get('protocol', 'genetlink')

        if schema_path is None:
            schema_path = os.path.dirname(os.path.dirname(spec_path)) + f'/{self.proto}.yaml'
        if schema_path:
            global jsonschema

            with open(schema_path, "r") as stream:
                schema = yaml.safe_load(stream)

            if jsonschema is None:
                jsonschema = importlib.import_module("jsonschema")

            jsonschema.validate(self.yaml, schema)

        self.attr_sets = collections.OrderedDict()
        self.msgs = collections.OrderedDict()
        self.req_by_value = collections.OrderedDict()
        self.rsp_by_value = collections.OrderedDict()
        self.ops = collections.OrderedDict()

        last_exception = None
        while len(self._resolution_list) > 0:
            resolved = []
            unresolved = self._resolution_list
            self._resolution_list = []

            for elem in unresolved:
                try:
                    elem.resolve()
                except (KeyError, AttributeError) as e:
                    self._resolution_list.append(elem)
                    last_exception = e
                    continue

                resolved.append(elem)

            if len(resolved) == 0:
                traceback.print_exception(last_exception)
                raise Exception("Could not resolve any spec element, infinite loop?")

    def new_attr_set(self, elem):
        return SpecAttrSet(self, elem)

    def new_operation(self, elem, req_val, rsp_val):
        return SpecOperation(self, elem, req_val, rsp_val)

    def add_unresolved(self, elem):
        self._resolution_list.append(elem)

    def _dictify_ops_unified(self):
        val = 0
        for elem in self.yaml['operations']['list']:
            if 'value' in elem:
                val = elem['value']

            op = self.new_operation(elem, val, val)
            val += 1

            self.msgs[op.name] = op

    def _dictify_ops_directional(self):
        req_val = rsp_val = 0
        for elem in self.yaml['operations']['list']:
            if 'notify' in elem:
                if 'value' in elem:
                    rsp_val = elem['value']
                req_val_next = req_val
                rsp_val_next = rsp_val + 1
                req_val = None
            elif 'do' in elem or 'dump' in elem:
                mode = elem['do'] if 'do' in elem else elem['dump']

                v = mode.get('request', {}).get('value', None)
                if v:
                    req_val = v
                v = mode.get('reply', {}).get('value', None)
                if v:
                    rsp_val = v

                rsp_inc = 1 if 'reply' in mode else 0
                req_val_next = req_val + 1
                rsp_val_next = rsp_val + rsp_inc
            else:
                raise Exception("Can't parse directional ops")

            op = self.new_operation(elem, req_val, rsp_val)
            req_val = req_val_next
            rsp_val = rsp_val_next

            self.msgs[op.name] = op

    def resolve(self):
        self.resolve_up(super())

        for elem in self.yaml['attribute-sets']:
            attr_set = self.new_attr_set(elem)
            self.attr_sets[elem['name']] = attr_set

        msg_id_model = self.yaml['operations'].get('enum-model', 'unified')
        if msg_id_model == 'unified':
            self._dictify_ops_unified()
        elif msg_id_model == 'directional':
            self._dictify_ops_directional()

        for op in self.msgs.values():
            if op.req_value is not None:
                self.req_by_value[op.req_value] = op
            if op.rsp_value is not None:
                self.rsp_by_value[op.rsp_value] = op
            if not op.is_async and 'attribute-set' in op:
                self.ops[op.name] = op
