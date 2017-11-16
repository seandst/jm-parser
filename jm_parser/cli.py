import glob
import os
import shutil

import click

from . import parsing
from . import scraping

DEFAULT_UC_BASE_URL = 'http://updates.jenkins-ci.org'
PLUGIN_BASE_URL = 'https://plugins.jenkins.io/'
_ignore_cache_decorator = click.option('--ignore-cache', '-i', default=False, is_flag=True)


def _uc_url_cb(ctx, param, value):
    # click callback to properly construct upstream UC urls if URL is not provided by user.
    if value is None:
        # uc_url is None, so we need to combine it with uc_version to construct the default value,
        # which is the versioned UC URL built on the default base
        return parsing.versioned_uc_url(DEFAULT_UC_BASE_URL, ctx.params['uc_version'])
    else:
        return value


def _jenkins_uc_options(func):
    # You're going to want to put this before other options/args so that there isn't any
    # inconsistency with where the uc version arg goes. Putting it before the others means it
    # always comes first, which in turn means the UI is (hopefully) consistent.
    func = click.argument('uc-version')(func)
    func = click.option('--uc-url', '-u', default=None, callback=_uc_url_cb,
                        help='URL to a specific update-center.json to parse.')(func)
    func = _ignore_cache_decorator(func)
    return func


@click.group()
def jm_cli_entry():
    """Jenkins Update Center and Plugin Management Utility"""
    pass


@jm_cli_entry.command()
@_jenkins_uc_options
@click.argument('plugin_name')
def depsolve(plugin_name, uc_url, uc_version, ignore_cache):
    """Given a plugin, recursively trace and print its dependencies based on upstream UC data"""
    uc_data = parsing.get_uc_data(uc_url, allow_prompt=True, ignore_cache=ignore_cache)
    available_plugins = parsing.get_available_plugins(uc_data)
    dependencies = parsing.depsolve(plugin_name, available_plugins)
    # print queried plugin first
    print('{}=={}'.format(dependencies[0].name, dependencies[0].version))
    # sort and print remaining deps
    for dep in sorted(dependencies[1:], key=lambda dep: dep.name):
        print('{}=={}'.format(dep.name, dep.version))


@jm_cli_entry.command(name="report-supported-versions")
def report_supported_versions():
    """Report supported versions of jenkins, as well as their supported start and end dates.

    This is mainly useful for human consumption.

    This command does not cache information fetched from the internet.
    """
    # doesn't take common options, always works on the same URL for all versions
    supported_versions = parsing.supported_versions()
    # date format str, because it's annoying to repeat it
    df = 'YYYY-MM-DD'
    # we only really care about the two most recent supported versions
    for supported_datestamp, xy_version, xyz_version, build_datestamp in supported_versions[-2:]:
        # I have no reason for why this output looks like yaml. Seemed like a good idea?
        print('- {}'.format(xy_version))
        print('  - xyz_version: {}'.format(xyz_version))
        print('  - support_begins: {}'.format(supported_datestamp.format(df)))
        print('  - support_ends: {}'.format(supported_datestamp.shift(years=1).format(df)))
        print('  - build_datestamp: {}'.format(build_datestamp.format(df)))


@jm_cli_entry.command(name='latest-xyz-version')
def latest_xyz_version():
    """Report latest supported x.y.z version of jenkins.

    This is mainly useful for script consumption, e.g. to set JENKINS_VERSION in test runs.

    This command does not cache information fetched from the internet.
    """
    supported_versions = parsing.supported_versions()
    supported_datestamp, xy_version, xyz_version, build_datestamp = supported_versions[-1]
    click.echo(xyz_version)


@jm_cli_entry.command(name="update-plugin-lists")
@_jenkins_uc_options
@click.argument('plugin_lists_dir')
@click.option('--dry-run', '-d', default=False, is_flag=True)
@click.option('--test', '-t', default=False, is_flag=True,
              help='Modify "*-test.txt" plugin lists instead of the default *.txt lists')
@click.option('--plugin', '-p', default=None,
              help='Only update a single plugin and its dependencies')
@click.option('--remove-missing', '-r', default=False, is_flag=True)
def update_plugin_lists(plugin_lists_dir, dry_run, test, plugin, remove_missing,
                        uc_url, uc_version, ignore_cache):
    """Update a plugin or plugins in a plugin lists dir from a Jenkins update center"""
    uc_data = parsing.get_uc_data(uc_url, allow_prompt=True, ignore_cache=ignore_cache)
    available_plugins = parsing.get_available_plugins(uc_data)
    missing_plugins = parsing.update_plugin_lists(plugin_lists_dir, available_plugins, dry_run,
                                                  test, remove_missing, plugin)
    if missing_plugins:
        if remove_missing:
            print('Some plugins are no longer available upstream and have been removed.')
        else:
            print('Some plugins are no longer available upstream.')
        for plugin_list_file in sorted(missing_plugins):
            print('Plugins no longer available listed in {}'.format(plugin_list_file))
            for plugin_name in missing_plugins[plugin_list_file]:
                print(' - {} ({})'.format(plugin_name, PLUGIN_BASE_URL + plugin_name))


@jm_cli_entry.command(name="compile-distribution")
@click.argument('plugin_lists_dir')
@click.argument('dist_dir')
@click.option('--xy-version', '-v', multiple=True, default=None,
              help='X.Y version to act on (acts on all supported versions if unset)')
def compile_distribution(plugin_lists_dir, dist_dir, xy_version):
    """Compile a cinch distribution for a supported x.y version of jenkins

    A jenkins distribution consists of the following files related to a given x.y version of
    jenkins:

    - .war files (all .z versions)
    - .rpm files (all .z versions)
    - plugin lists, including default, optional, core, and blacklist

    """
    if not xy_version:
        xy_versions = []
        # supported versions comes from earliest to newest, users would probably appreciate seeing
        # the newest first, so reverse it
        supported_versions = reversed(parsing.supported_versions())
        for supported_datestamp, xy_version, xyz_version, build_datestamp in supported_versions:
            if xy_version not in xy_versions:
                xy_versions.append(xy_version)
    else:
        # already iterable to multiple=True in click option def
        xy_versions = xy_version

    for xy_version in xy_versions:
        # TODO: this is too much work to do in the cli side, so break this out into lib functions
        # ensure plugin lists dir for xy_version exists
        xy_plugin_lists_dir = os.path.join(plugin_lists_dir, xy_version)
        xy_plugin_lists_glob = os.path.join(xy_plugin_lists_dir, '*.txt')
        if not os.path.exists(xy_plugin_lists_dir):
            raise RuntimeError('Unable to find {} dir in {}'.format(xy_version, plugin_lists_dir))
        # ensure output dirs for plugins, rpm, and war exist in dist dir
        for subdir in ('plugins', 'rpm', 'war'):
            dist_subdir = os.path.join(dist_dir, subdir)
            os.makedirs(dist_subdir, exist_ok=True)
        plugin_xy_dir = os.path.join(dist_dir, 'plugins', xy_version)
        os.makedirs(plugin_xy_dir, exist_ok=True)
        # and finally, copy over the current plugin txts for version
        for txt_file in glob.glob(xy_plugin_lists_glob):
            # there are other text files in plugin list dirs that we don't care about (e.g. core)
            if os.path.basename(txt_file) not in ('default.txt', 'optional.txt'):
                continue
            dest = os.path.join(plugin_xy_dir, os.path.basename(txt_file))
            click.echo('Copying {} plugin list to {}'.format(txt_file, dest))
            shutil.copyfile(txt_file, dest)
        # After all the prep work and copy of local stuff, scrape rpms and wars
        scraping.scrape_for_versions(xy_versions, dist_dir, allow_prompt=True)
