"""Shared test helper: a minimal YAML loader that understands
CloudFormation's short-form intrinsic function tags (!Ref, !GetAtt, ...)
well enough to parse a template structurally, without a full CFN-aware
YAML library. Used by structural (offline-only) infrastructure tests.

Leading underscore keeps pytest from collecting this as a test module.
"""

from pathlib import Path

import yaml


class CfnLoader(yaml.SafeLoader):
    pass


def _construct_ref(loader, node):
    return {"Ref": loader.construct_scalar(node)}


def _construct_get_att(loader, node):
    # CloudFormation normalizes the short-form `!GetAtt Foo.Bar` into
    # {"Fn::GetAtt": ["Foo", "Bar"]} — replicate that here so tests can
    # compare against the same shape the AWS CLI/console would produce.
    value = loader.construct_scalar(node)
    logical_id, _, attribute = value.partition(".")
    return {"Fn::GetAtt": [logical_id, attribute]}


def _construct_generic_fn(tag_suffix):
    def _construct(loader, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_mapping(node)
        return {f"Fn::{tag_suffix}": value}

    return _construct


CfnLoader.add_constructor("!Ref", _construct_ref)
CfnLoader.add_constructor("!GetAtt", _construct_get_att)
for _suffix in (
    "Sub",
    "Join",
    "Select",
    "Split",
    "FindInMap",
    "GetAZs",
    "ImportValue",
    "Base64",
    "Cidr",
    "If",
    "Not",
    "Equals",
    "And",
    "Or",
    "Condition",
):
    CfnLoader.add_constructor(f"!{_suffix}", _construct_generic_fn(_suffix))


def load_cfn_template(path: Path) -> dict:
    with open(path, "r") as f:
        return yaml.load(f, Loader=CfnLoader)
