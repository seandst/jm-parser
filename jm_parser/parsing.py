import click
import gzip
import hashlib
import json
import os
import time
import warnings
from collections import defaultdict
from distutils.version import LooseVersion
from xml.etree.ElementTree import fromstring

import appdirs
import arrow

from .exceptions import PluginNotFound
from .plugin import JenkinsPlugin

try:
    # py3
    from urllib.request import urlopen
except ImportError:
    # py2
    from urllib2 import urlopen

UC_JSON = 'update-center.json'
VERSION_1651 = LooseVersion('1.651')
PLUGIN_PROD_LISTS = ('default.txt', 'optional.txt')
PLUGIN_TEST_LISTS = ('default-test.txt', 'optional-test.txt')


def versioned_uc_url(uc_base_url, uc_version=None):
    if uc_version is not None:
        join_args = (uc_base_url, uc_version, UC_JSON)
    else:
        join_args = (uc_base_url, UC_JSON)
    return '/'.join(join_args)


def setup_cache_dir(uc_url):
    user_cache_dir = appdirs.user_cache_dir('jm_parser')
    # rather than dealing with encoding the UC URL, or otherwise parsing it,
    # just hash it and use the hash to make the dir to stash the cache.
    h = hashlib.md5(uc_url.encode('utf8'))
    uc_cache_dir = os.path.join(user_cache_dir, h.hexdigest())
    os.makedirs(uc_cache_dir, exist_ok=True)
    return uc_cache_dir


def get_uc_data(uc_url, allow_prompt=False, ignore_cache=False):
    # retrieve, parse, and save remote UC JSON
    # it is retrieved into memory first to ensure it is parseable, and then re-saved locally
    # so that the brace finding doesn't have to happen every time it is loaded from cache
    uc_cache_dir = setup_cache_dir(uc_url)
    uc_cache_file = os.path.join(uc_cache_dir, UC_JSON)

    # sentinel value, by default the cache is not updated unless certain conditions are met in the
    # following conditional chain
    download_uc = False

    # now do a bunch of stuff to decide if we need to download a new UC file
    # or if we can just use the cache.
    try:
        uc_cache_stat = os.stat(uc_cache_file)
        # is the cache mtime less recent than a day ago?
        if uc_cache_stat.st_size == 0:
            # file size is zero, indicates a previous problem when trying to save the json
            # TODO: Consider actually attempting to parse the cache file here and checking for
            #       basic expectations (e.g. has a 'plugins' key), redownloading if the checks fail
            if allow_prompt:
                click.echo('Update Center cache is invalid, updating cache.')
            download_uc = True
        elif ignore_cache:
            # user has explicitly request that the cache be ignored
            download_uc = True
        elif uc_cache_stat.st_mtime < (time.time() - 86400):
            # cache is older than one day, prompt user to update if allowed
            if allow_prompt:
                download_uc = click.confirm('Update Center cache is outdated, download again?',
                                            default=True)
            else:
                # no prompting allowed, update the cache
                download_uc = True
    except FileNotFoundError:
        # Well that was easy: No cache file found, so we need to download the file
        download_uc = True

    if download_uc:
        if allow_prompt:
            # don't leak prints in library code unless prompting is allowed
            print('Updating UC JSON cache from {}...'.format(uc_url))
        response = urlopen(uc_url)
        # The UC JSON isn't valid JSON. The actual JSON contents are wrapped in a
        # JavaScript function call, so a little parsing is required to find the
        # opening and closing braces of the JSON inside for loading.
        body = response.read().decode('utf8')
        uc_data = json.loads(body[body.find('{'):body.rfind('}') + 1])
        # stash the cache
        json.dump(uc_data, open(uc_cache_file, 'w'))
    else:
        if allow_prompt:
            print('Loading UC JSON from cache of {}...'.format(uc_url))
        uc_data = json.load(open(uc_cache_file, 'r'))

    # ...and finally
    return uc_data


def get_available_plugins(uc_data):
    available_plugins = {}
    # reduce each plugin in this list to its JenkinsPlugin repr, along with
    # the JenkinsPlugin repr of each of its dependencies
    for plugin_name, plugin_data in uc_data['plugins'].items():
        jp = JenkinsPlugin(plugin_name, plugin_data['version'])
        dependencies = []
        for dep in plugin_data['dependencies']:
            if dep['optional']:
                # ignore optional deps, since this script is looking for the
                # absolutely minimum required deps for a given plugin
                continue
            dependencies.append(JenkinsPlugin(dep['name'], dep['version']))
        available_plugins[jp] = dependencies
    return available_plugins


def get_latest_version(plugin_name, plugin_iterable):
    """Get the latest available version for named plugin"""
    if JenkinsPlugin(plugin_name, '0') not in plugin_iterable:
        # short out using equality/hash identity if plugin is not in iterable
        raise PluginNotFound(plugin_name)

    latest_plugin = None
    for plugin in filter(lambda plugin: plugin.name == plugin_name, plugin_iterable):
        if latest_plugin is None or plugin > latest_plugin:
            latest_plugin = plugin
    return latest_plugin


def supported_date_generator():
    # The start date of the 6-month rolling jenkins support cycle is Sept 1, 2017, with Jenkins
    # 2.60. Anything before that is somewhat undefined, most likely running 1.651.3, so for
    # simplicity this generator starts out 6 months before the Sept 1 start date, which will be
    # manually paired up with 1.651. Supported versions will be calculated based on the Sept 1
    # thereafter.
    date = arrow.Arrow(2017, 3, 1)
    while True:
        # Ensure we don't report supporting release dates in the future
        # An exception is made for releases prior to the Sept 1 target date when the rolling
        # support cycle begins, and that bit of this conditional can be removed on or after this
        # date.
        if date <= arrow.Arrow(2017, 9, 1) or date <= arrow.utcnow():
            yield date
        else:
            break
        date = date.shift(months=6)


def depsolve(plugin_name, available_plugins, dependencies=None):
    """Given a plugin, recursively solve its dependencies.

    Args:
        plugin_name: name of the plugin in the update center being parsed
        available_plugins: dict of JenkinsPlugin: [list of JenkinsPlugin dependencies]
        dependencies: optional list of dependencies recursively updated in-place
    """
    if dependencies is None:
        # If no dependencies are passed in, initialize the list with the first dependency,
        # which is the plugin being depsolved itself.
        dependencies = [get_latest_version(plugin_name, available_plugins)]

    # find the dependencies for the plugin we're trying to solve
    for plugin, plugin_dependencies in available_plugins.items():
        if plugin.name == plugin_name:
            break
    else:
        raise PluginNotFound(plugin_name)

    for plugin in plugin_dependencies:
        for i, dep in enumerate(dependencies):
            if plugin == dep:
                if plugin > dep:
                    # If this requirement is newer than an existing one, update it in-place.
                    dependencies[i] = plugin
                break
        else:
            # If this plugin has not yet been seen in the dependencies list, add it
            dependencies.append(plugin)

        # recursively resolve the dependencies for this plugin
        depsolve(plugin.name, available_plugins, dependencies)
    for plugin in dependencies:
        # Warn if the resolved dependies result in a newer plugin than this UC can provide
        warn_if_newer_plugin(plugin, available_plugins)
    return dependencies


def supported_versions():
    # So...we need a way to get at the currently available versions of the Jenkins LTS releases, as
    # well as machine-parseable dates related to them. This approach is to parse the repo metadata
    # from the "rpm-stable" repo to get at the data, which is...circuitous?
    repo_base = 'https://pkg.jenkins.io/redhat-stable/'

    # so, first get the repo metadata to find the "primary" XML (package list)
    repomd_response = urlopen(repo_base + 'repodata/repomd.xml')
    repomd = fromstring(repomd_response.read().decode('utf8'))
    # etree's namespaces make this a little sad to look at :(
    primary_location = repomd.find(
        './{http://linux.duke.edu/metadata/repo}data[@type="primary"]'
        '/{http://linux.duke.edu/metadata/repo}location'
    )

    # Now, use the primary xml href to grab the package list
    primary_response = urlopen(repo_base + primary_location.attrib['href'])
    primary_gzip = gzip.decompress(primary_response.read())
    primary_xml = fromstring(primary_gzip.decode('utf8'))
    # We only care about 'jenkins' rpms, which I'm pretty sure is all that's in this repo.
    # Ideally, we'd be able to express exactly this with an XPath selector and not have to filter
    # these in Python, but elementtree's xpath parser is extremely limited and can't do this.
    packages = primary_xml.findall('./{http://linux.duke.edu/metadata/common}package[@type="rpm"]')
    interesting_versions = defaultdict(list)
    for package in packages:
        if package.find('./{http://linux.duke.edu/metadata/common}name').text != 'jenkins':
            # not jenkins, skip it
            continue

        version_str = package.find('./{http://linux.duke.edu/metadata/common}version').get('ver')
        version = LooseVersion(version_str)
        if version < VERSION_1651:
            # older than 1.651, don't care
            continue

        build_date_str = package.find('./{http://linux.duke.edu/metadata/common}time').get('build')
        build_date = arrow.get(build_date_str)
        xy_version = '.'.join(version_str.split('.')[:2])
        interesting_versions[xy_version].append((version, build_date))

    # the ordering in the XML appears to be oldest to newest, but for reliable processing later,
    # the list of interesting versions for a given xy version should be sorted to ensure the x.y.1
    # version is always the first element of the interesting versions list
    for xy_version in interesting_versions:
        interesting_versions[xy_version] = sorted(interesting_versions[xy_version],
                                                  key=lambda v: v[0])

    # now that the values are sorted, get to work on the sorted interesting versions themselves
    supported_versions = []
    date_generator = supported_date_generator()

    # seed supported versions with 1.651.3 at the earliest supported date
    # supported versions is a tuple of:
    #   (supported_datestamp, xy_version, xyz_version, build_datestamp)
    supported_versions.append(
        (next(date_generator), '1.651', *interesting_versions['1.651'][-1])
    )

    # now consume supported date generator to find the most recent x.y.1 version before a supported
    # date to get the x.y version for that period of support, but report the latest x.y.z release
    # as being supported. Example to make that make sense: 2.60.1 is the latest release that
    # happened before the Sept 1, 2017 date, but 2.60.2 has been release since then. So we parse
    # this list to find the first LTS release before the date, but report the most recent "Z"
    # release since our support policy
    for supported_date in date_generator:
        most_recent_offset = 0
        for xy_version in sorted(interesting_versions, key=lambda v: LooseVersion(v)):
            first_build = interesting_versions[xy_version][0][1]
            build_stamp_offset = (supported_date - first_build).total_seconds()
            if build_stamp_offset < 0:
                # build stamp is more recent than supported date, break out.
                # the current value of "xy_version" should be used
                break
            elif build_stamp_offset < most_recent_offset:
                most_recent_offset = build_stamp_offset

        supported_versions.append(
            (supported_date, xy_version, *interesting_versions[xy_version][-1])
        )

    return supported_versions


def find_plugin(plugin_name, plugin_iterable):
    for avail_plugin in plugin_iterable:
        if avail_plugin.name == plugin_name:
            return avail_plugin
    else:
        return None


def warn_if_newer_plugin(plugin, plugin_iterable):
    """Warn if a given plugin version in unavailable in a given iterable.

    Useful to see if a solved list of dependencies includes plugins that are not available for
    a given jenkins distribution version.

    This shouldn't happen if the UC being parsed is entirely self-consistent, but there are cases
    they upstream UCs have inconsistent dependencies and caution is warranted.
    """
    avail_plugin = find_plugin(plugin.name, plugin_iterable)
    if avail_plugin is not None:
        if plugin > avail_plugin:
            warnings.warn(
                '{} {} in is newer than available version {}'.format(
                    plugin.name, plugin.version, avail_plugin.version),
                RuntimeWarning, stacklevel=2)


def _refine_plugin_list(plugin_list, seen_plugins=[]):
    # not in love with this function name, but the idea's pretty simple:
    # given a list of plugins, return a similar list with duplicated removed,
    # including only the highest versions of each plugin seen. This is useful
    # for processing updated plugin lists that bring in new dependencies, since
    # many plugins in a given list may request the same plugin name at different
    # versions; this refines a list made with that in mind down to the highest
    # requested version of a given plugin. Also supports filtering out already-seen
    # plugins
    refined_plugins = {}
    for plugin in plugin_list:
        if plugin.name in seen_plugins:
            continue
        try:
            # overwrite the value in the refined plugin list if it's there at a lower version...
            if refined_plugins[plugin.name] < plugin:
                refined_plugins[plugin.name] = plugin
        except KeyError:
            # ...or just do the initial assignment if it's not there at all
            refined_plugins[plugin.name] = plugin
    return list(refined_plugins.values())


def _process_plugin_list(plugin_list_file, available_plugins, remove_missing=False):
    # given a plugin list txt file, output all listed plugins

    # track removed plugins for reporting to users
    removed_plugins = set()

    with open(plugin_list_file, 'r') as infile:
        lines_in = infile.readlines()
    plugins_out = []
    for line in lines_in:
        try:
            plugin = JenkinsPlugin(*line.strip().split('=='))
        except (TypeError, ValueError):
            # no '==' in line to split on, entire line is plugin name
            # instantiate with version of 0 to ensure plugin gets updated at some point with a
            # "real" version in later processing
            plugin = JenkinsPlugin(line.strip(), '0')

        if not plugin.name:
            # plugin name is blank, maybe a blank line in a plugin list
            continue
        # check if plugin is not available upstream (removed or added in later version)
        avail_plugin = find_plugin(plugin.name, available_plugins)
        if avail_plugin is not None:
            warn_if_newer_plugin(plugin, available_plugins)
        else:
            removed_plugins.add(plugin.name)
            if remove_missing:
                # If removing missing plugins from output, continue here to prevent this plugin
                # from being added to the output list.
                continue
        plugins_out.append(plugin)
    return plugins_out, removed_plugins


def _write_plugin_list(plugin_list_file, plugin_list):
    with open(plugin_list_file, 'w') as outfile:
        lines_out = sorted([plugin.plugin_list_entry for plugin in plugin_list])
        outfile.writelines(lines_out)


def update_plugin_lists(plugin_lists_dir, available_plugins, dry_run, test,
                        remove_missing=False, update_plugin_name=None):
    # given an available_plugins dict, update all plugin lists in given dir
    # if a dependencies list is passed in, only update the specific dependencies,
    # and then only do it if the current version in the list is too low
    if test:
        lists = [os.path.join(plugin_lists_dir, plugin_list) for plugin_list in PLUGIN_TEST_LISTS]
    else:
        lists = [os.path.join(plugin_lists_dir, plugin_list) for plugin_list in PLUGIN_PROD_LISTS]

    # order matters here, any plugin in a list should include its dependencies in that list unless
    # that plugin has been seen in a previous list. This is also what seen_plugins is tracking
    seen_plugins = set()
    missing_plugins = dict()
    for plugin_list_file in lists:
        plugin_list, removed_plugins = _process_plugin_list(
            plugin_list_file, available_plugins, remove_missing)
        # a single plugin is named for updating, so first we need to find what file it's in, and
        # update the plugin in that file, as well as add dependencies for it to that same file.
        # if the plugin is not in any file, add it to optional
        if update_plugin_name is not None:
            # not-very-readable logic to determine if the plugin being updated is in the current
            # file, if it isn't in any file and we're processing optional (so it needs to be
            # added), or if we're processing optional but it's already been added to default and
            # should be ignored in this loop
            if find_plugin(update_plugin_name, plugin_list) or (
                    (plugin_list_file.endswith('optional.txt') or
                     plugin_list_file.endswith('optional-test.txt')) and
                    update_plugin_name not in seen_plugins):
                plugin_list.extend(depsolve(update_plugin_name, available_plugins))
        else:
            # updating all plugins in all lists, process dependencies to ensure any new
            # dependencies are included in the output
            extended_deps = []
            for plugin in plugin_list:
                # since depsolve includes the latest version of a given plugin as its first result,
                # this also ends up updating every plugin in the list, which is what we want
                try:
                    extended_deps.extend(depsolve(plugin.name, available_plugins))
                except PluginNotFound:
                    pass
            plugin_list.extend(extended_deps)

        # now refine the plugin list to get us the list of all the latest plugins and their
        # dependencies and write out the new file
        plugin_list = _refine_plugin_list(plugin_list, seen_plugins)
        if not dry_run:
            _write_plugin_list(plugin_list_file, plugin_list)

        # update seen plugin list so plugins in this file aren't included in later files
        seen_plugins.update(set(plugin.name for plugin in plugin_list))
        missing_plugins[plugin_list_file] = removed_plugins

    return missing_plugins
