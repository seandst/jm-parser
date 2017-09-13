import hashlib
from collections import namedtuple
from distutils.version import LooseVersion


class JenkinsPlugin(namedtuple('_JP', ('name', 'version'))):
    """A namedtuple with fields "id" and "version"

    This namedtuple can be compared with other namedtuples of the same type
    for easy sorting. Comparison with other types will result in errors being
    raised.

    For comparison, equality matches against the plugin name, while
    greater-than/less-than comparisons match against the plugin version. This
    is intended to make checking and updating dependencies during runtime not
    be excessively tedious.
    """
    def __eq__(self, other):
        # Match equality on plugin name only to make it easy to use the "in"
        # operator to find this plugin in a list
        return self.name.lower() == other.name.lower()

    def __hash__(self):
        # implement __hash__ to make this tuple usable as a dict key
        # Similar to equality, this objects hash identity is based on its name,
        # and excludes the version piece, allowing "key in dict" lookups to
        # succeed when key is a plugin with a different version.
        hash_bytes = self.name.encode()
        return int(hashlib.md5(hash_bytes).hexdigest(), 16)

    # Rich comparison functions delegate version comparison to LooseVersion
    def __lt__(self, other):
        return LooseVersion(self.version) < LooseVersion(other.version)

    def __le__(self, other):
        return LooseVersion(self.version) <= LooseVersion(other.version)

    def __gt__(self, other):
        return LooseVersion(self.version) > LooseVersion(other.version)

    def __ge__(self, other):
        return LooseVersion(self.version) >= LooseVersion(other.version)

    @property
    def plugin_list_entry(self):
        # string of an entire line (including newline) representing this plugin in a
        # plugin list file, e.g. 'name==version\n'
        return '{}=={}\n'.format(self.name, self.version)
