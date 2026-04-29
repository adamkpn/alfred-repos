#!/usr/bin/env python3
# encoding: utf-8
#
# Copyright (c) 2022 Thomas Harr <xDevThomas@gmail.com>
# Copyright (c) 2014 Dean Jackson <deanishe@deanishe.net>
#
# MIT Licence. See http://opensource.org/licenses/MIT
#
# Created on 2013-11-04
#

"""repos.py [command] [options] [<query>] [<path>]

Find, open and search Git repos on your system.

Usage:
    repos.py search [<query>]
    repos.py settings
    repos.py update
    repos.py open <appkey> <path>

Options:
    -h, --help      Show this message

"""


import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import namedtuple

from workflow import ICON_INFO, ICON_WARNING, Workflow
from workflow.background import is_running, run_in_background
from workflow.update import Version
from workflow.notify import notify

# How often to check for new/updated repos
DEFAULT_UPDATE_INTERVAL = 180  # minutes

# GitHub repo for self-updating
UPDATE_SETTINGS = {'github_slug': 'harrtho/alfred-repos'}

# GitHub Issues
HELP_URL = 'https://github.com/harrtho/alfred-repos/issues'

# Icon shown if a newer version is available
ICON_UPDATE = 'update-available.png'

# Available modifier keys
# MODIFIERS = ('cmd', 'alt', 'ctrl', 'shift', 'fn')

# These apps will be passed the remote repo URL instead
# of the local directory path
BROWSERS = [
    'Browser',  # default browser
    'Google Chrome',
    'Firefox',
    'Safari',
    'WebKit',
]

DEFAULT_SEARCH_PATH = '~/delete/this/example'
DEFAULT_SETTINGS = {
    'search_dirs': [{
        'path': DEFAULT_SEARCH_PATH,
        'depth': 2,
        'name_for_parent': 1,
        'excludes': ['tmp', 'bad/smell/*']
    }],
    'global_exclude_patterns': [],
    'app_default': 'Finder',
    'app_cmd': 'Terminal',
    'app_alt': None,
    'app_ctrl': None,
    'app_shift': None,
    'app_fn': None,
    # If set, `reposettings` runs [settings_editor] + settings_editor_args + [settings.json]
    # instead of `open` (default association). Example: "zed" with ["-n"] for a new workspace.
    'settings_editor': None,
    'settings_editor_args': None,
    # Optional: map app name (same string as app_*) -> args for `open --args` when opening a repo
    # folder (e.g. Zed ``-n`` for a new workspace). See README.
    'repos_open_args_for_apps': None,
}

# Will be populated later
log = None


Repo = namedtuple('Repo', 'name path')


class AttrDict(dict):
    """Access dictionary keys as attributes."""

    def __init__(self, *args, **kwargs):
        """Create new dictionary."""
        super(AttrDict, self).__init__(*args, **kwargs)
        # Assigning self to __dict__ turns keys into attributes
        self.__dict__ = self


def is_defaults(d):
    """Return ``True`` if settings are do-nothing defaults.

    Args:
        d (dict): Workflow settings
    """
    dirs = d.get('search_dirs') or []
    return len(dirs) == 1 and dirs[0]['path'] == DEFAULT_SEARCH_PATH


def settings_updated():
    """Test whether settings file is newer than repos cache.

    Returns:
        bool: ``True`` if ``settings.json`` is newer than the repos cache.

    """
    cache_age = wf.cached_data_age('repos')
    settings_age = time.time() - os.stat(wf.settings_path).st_mtime
    log.debug('cache_age=%0.2f, settings_age=%0.2f', cache_age, settings_age)
    return settings_age < cache_age


def join_english(items):
    """Join a list of unicode objects with commas and/or 'and'."""
    if isinstance(items, str):
        return items

    if len(items) == 1:
        return items[0]

    elif len(items) == 2:
        return ' and '.join(items)

    return ', '.join(items[:-1]) + ' and {}'.format(items[-1])


def get_apps():
    """Load applications configured in settings.

    Each value may be a string for a single app or a list for
    multiple apps.

    Returns:
        dict: Modkey to application mapping.

    """
    apps = {}
    for key, app in wf.settings.items():
        if not key.startswith('app_'):
            continue

        key = key[4:]
        if isinstance(app, list):
            app = app[:]
        apps[key] = app

    if not apps.get('default'):  # Things will break if this isn't set
        apps['default'] = 'Finder'

    return apps


def get_repos(opts):
    """Load repos from cache, triggering an update if necessary.

    Args:
        opts (AttrDict): CLI options

    Returns:
        list: Sequence of `Repo` tuples.

    """
    # Load data, update if necessary
    if not wf.cached_data_fresh('repos', max_age=opts.update_interval):
        do_update()
    repos = wf.cached_data('repos', max_age=0)

    if not repos:
        do_update()
        return []

    # Check if cached data is old version
    if isinstance(repos[0], str):
        do_update()
        return []

    return repos


def repo_url(path):
    """Return repo URL extracted from `.git/config`.

    Args:
        path (str): Path to git repo.

    Returns:
        str: URL of remote specified in the remote_name setting. Defaults to origin.

    """
    remote_name = wf.settings.get('remote_name')
    # Check if key does not exist or value is set to None
    if remote_name is None:
        remote_name = 'origin'

    remotes = subprocess.check_output(['git', 'remote'], cwd=path).decode('utf-8').splitlines()
    log.debug('remotes=%s', remotes)
    if remote_name not in remotes:
        notify('No remote named {}'.format(remote_name), 'Check your settings', 'Available remotes: {}'.format(', '.join(remotes)), 'Sosumi')
        log.error('No remote named %s in %s', remote_name, path)
        return None

    url = subprocess.check_output(['git', 'config', 'remote.' + remote_name + '.url'],
                                  cwd=path).decode('utf-8')
    url = re.sub(r'(^.+@)|(^https://)|(^git://)|(.git$)', '', url)
    return 'https://' + re.sub(r':', '/', url).strip()


def _extra_args_for_open_app(app_name):
    """Return argv tokens to pass to an app after ``open ... --args``.

    ``settings.json`` key ``repos_open_args_for_apps`` maps the same
    application names you use in ``app_default`` / ``app_cmd`` / … to a list
    of strings (or one string, split with :func:`shlex.split`). Lookup is
    case-insensitive, so ``zed`` matches ``Zed``.

    Used when opening a repo directory so flags like Zed's ``-n`` (new
    workspace) are applied; plain ``open -a`` cannot pass those flags.

    Args:
        app_name (str): Application name from settings.

    Returns:
        list[str]: Extra arguments for the target app, or empty list.
    """
    raw = wf.settings.get('repos_open_args_for_apps')
    if not raw or not isinstance(raw, dict):
        return []
    target = str(app_name).lower()
    for key, val in raw.items():
        if str(key).lower() != target:
            continue
        if val is None:
            return []
        if isinstance(val, list):
            return [str(x) for x in val]
        if isinstance(val, str):
            return shlex.split(val)
        log.warning('repos_open_args_for_apps[%r] must be a list or string', key)
        return []
    return []


def _canonical_application_name(app):
    """Map names so ``open -a`` resolves (e.g. CLI name ``zed`` → bundle ``Zed``)."""
    s = str(app).strip()
    if s.lower() == 'zed':
        return 'Zed'
    return app


def _open_folder_with_app(app, path):
    """Open directory ``path`` with a local application (not a browser URL)."""
    extra = _extra_args_for_open_app(app)
    canonical = _canonical_application_name(app)

    if extra:
        # Prefer a real CLI when the app name is a single token: `open --args`
        # is unreliable for Zed and some editors; `zed -n path` works.
        cli = None
        if ' ' not in str(app).strip():
            cli = _resolve_editor_executable(str(app).strip())
        if cli:
            cmd = [cli] + extra + [path]
            log.info('opening folder via CLI %r', cmd)
            subprocess.call(cmd)
            return

        cmd_open = ['open', '-n', '-a', canonical, '--args'] + extra + [path]
        log.debug('open folder cmd=%r', cmd_open)
        rc = subprocess.call(cmd_open)
        if rc != 0:
            log.warning('open with --args exited %s; trying plain open -a', rc)
            subprocess.call(['open', '-a', canonical, path])
        return

    subprocess.call(['open', '-a', canonical, path])


def do_open(opts):
    """Open repo in the specified application(s).

    Args:
        opts (AttrDict): CLI options.

    Returns:
        int: Exit status.

    """
    all_apps = get_apps()
    apps = all_apps.get(opts.appkey)
    if apps is None:
        print('App {} not set. Use `reposettings`'.format(opts.appkey))
        return 0

    if not isinstance(apps, list):
        apps = [apps]

    for app in apps:
        if app in BROWSERS:
            url = repo_url(opts.path)
            if url:
                log.info('opening %s with %s ...', url, app)
                if app == 'Browser':
                    subprocess.call(['open', url])
                else:
                    subprocess.call(['open', '-a', app, url])
        else:
            log.info('opening %s with %s ...', opts.path, app)
            _open_folder_with_app(app, opts.path)


def _path_for_editor_lookup():
    """PATH Alfred should use to find CLI editors (Alfred often omits Homebrew, etc.)."""
    ordered = []
    seen = set()
    for extra in (
        '/opt/homebrew/bin',
        '/usr/local/bin',
        os.path.expanduser('~/.local/bin'),
    ):
        if os.path.isdir(extra):
            seen.add(extra)
            ordered.append(extra)
    for part in os.environ.get('PATH', '').split(os.pathsep):
        if part and part not in seen and os.path.isdir(part):
            seen.add(part)
            ordered.append(part)
    return os.pathsep.join(ordered)


def _resolve_editor_executable(editor):
    """Resolve ``settings_editor`` to an executable path.

    Alfred workflows often run with a minimal ``PATH``, so bare names like
    ``zed`` are not found unless installed under a standard system path.

    Args:
        editor (str): Value from ``settings_editor`` (trimmed).

    Returns:
        str | None: Executable path, or ``None`` if not found / not executable.
    """
    if not editor:
        return None
    if os.path.isabs(editor):
        return editor if os.path.isfile(editor) and os.access(editor, os.X_OK) else None
    if os.path.sep in editor:
        p = os.path.abspath(editor)
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    found = shutil.which(editor, path=_path_for_editor_lookup())
    if found:
        return found
    # Zed: the installable `zed` command is the `cli` helper inside the bundle,
    # not the main `zed` GUI binary. Alfred often lacks /usr/local/bin on PATH.
    if str(editor).lower() == 'zed':
        for root in ('/Applications', os.path.expanduser('~/Applications')):
            cli = os.path.join(root, 'Zed.app', 'Contents', 'MacOS', 'cli')
            if os.path.isfile(cli) and os.access(cli, os.X_OK):
                return cli
    return None


def _settings_editor_command():
    """Build argv to open ``settings.json`` for :func:`do_settings`.

    Returns:
        tuple[list[str] | None, str | None]: ``(argv, None)`` when a custom
        editor command should run; ``(None, reason)`` to fall back to ``open``
        (reason is for logging / notifications).
    """
    raw = wf.settings.get('settings_editor')
    if raw is None:
        return None, None
    if isinstance(raw, str):
        editor = raw.strip()
    else:
        log.warning('settings_editor must be a string; using default open')
        return None, None
    if not editor:
        return None, None

    args = wf.settings.get('settings_editor_args')
    if args is None:
        extra = []
    elif isinstance(args, str):
        extra = shlex.split(args)
    elif isinstance(args, list):
        extra = [str(a) for a in args]
    else:
        log.warning('settings_editor_args must be a list or string; ignoring')
        extra = []

    resolved = _resolve_editor_executable(editor)
    if not resolved:
        log.warning('settings_editor %r not found or not executable', editor)
        return None, editor

    return [resolved] + extra + [wf.settings_path], None


def do_settings():
    """Open ``settings.json`` in configured or default editor.

    If ``settings_editor`` is set in workflow settings, runs that executable
    with optional ``settings_editor_args`` followed by the settings file path.
    Otherwise uses ``open`` (macOS default application for ``.json``).

    Args:
        opts (AttrDict): CLI options.

    Returns:
        int: Exit status.

    """
    cmd, missing_editor = _settings_editor_command()
    if cmd:
        log.info('opening settings with %r', cmd)
        try:
            rc = subprocess.call(cmd)
        except OSError as err:
            log.exception('failed to run settings editor')
            notify(
                'reposettings failed',
                str(err),
                'Could not run the configured editor. Opened settings with the default app instead.',
                'Sosumi',
            )
            subprocess.call(['open', wf.settings_path])
            return 0
        if rc != 0:
            log.warning('settings editor exited with code %s', rc)
        return 0

    if missing_editor:
        notify(
            'reposettings: editor not found',
            missing_editor,
            'Set settings_editor to the full path of the CLI (Alfred has a minimal PATH). Opened settings with the default app instead.',
            'Sosumi',
        )
    subprocess.call(['open', wf.settings_path])
    return 0


def do_update():
    """Update cached list of git repos.

    Args:
        opts (AttrDict): CLI options.

    Returns:
        int: Exit status.

    """
    run_in_background('update', ['/usr/bin/env', 'python3', 'update.py'])
    return 0


def do_search(repos, opts):
    """Filter list of repos and show results in Alfred.

    Args:
        repos (list): Sequence of ``Repo`` tuples.
        opts (AttrDict): CLI options.

    Returns:
        int: Exit status.

    """
    apps = get_apps()
    subtitles = {}
    valid = {}
    for key, app in apps.items():
        if not app:
            subtitles[key] = ('App for ' + key + ' not set. '
                              'Use `reposettings` to set it.')
            valid[key] = False
        else:
            subtitles[key] = 'Open in {}'.format(join_english(app))
            valid[key] = True

    if opts.query:
        repos = wf.filter(
            opts.query,
            repos,
            lambda t: '{} {}'.format(t[0], t[1]),
            min_score=30,
        )
        log.info('%d/%d repos match `%s`', len(repos), len(repos), opts.query)

    if not repos:
        wf.add_item('No matching repos found', icon=ICON_WARNING)

    home = os.environ['HOME']
    for r in repos:
        log.debug(r)
        pretty_path = r.path.replace(home, '~')
        # Title = repo name; subtitle = path (result subtext). "Open in …" for
        # modifier keys is on modifier rows only (see add_modifier below).
        title = r.name
        subtitle = pretty_path
        match = '{} {}'.format(r.name, r.path)

        # Check if Icon Path Exists else use default
        icon = 'icon.png'
        if os.path.isfile(os.path.dirname(r.path) + '/' + '.alfred-repos-icon.png'):
            icon = os.path.dirname(r.path) + '/' + '.alfred-repos-icon.png'

        it = wf.add_item(
            title,
            subtitle,
            arg=r.path,
            uid=r.path,
            valid=valid.get('default', False),
            type='file',
            icon=icon,
            largetext=r.path,
            copytext=r.path,
            match=match,
        )
        it.setvar('appkey', 'default')

        for key in apps:
            if key == 'default':
                continue
            mod = it.add_modifier(key.replace('_', '+'),
                                  pretty_path + '  //  ' + subtitles[key],
                                  arg=r.path, valid=valid[key])
            mod.setvar('appkey', key)

    wf.send_feedback()
    return 0


def parse_args():
    """Extract options from CLI arguments.

    Returns:
        AttrDict: CLI options.

    """
    from docopt import docopt

    args = docopt(__doc__, wf.args)

    log.debug('args=%r', args)

    update_interval = int(os.getenv('UPDATE_EVERY_MINS',
                                    DEFAULT_UPDATE_INTERVAL)) * 60

    opts = AttrDict(
        query=(args.get('<query>') or '').strip(),
        path=args.get('<path>'),
        appkey=args.get('<appkey>') or 'default',
        update_interval=update_interval,
        do_search=args.get('search'),
        do_update=args.get('update'),
        do_settings=args.get('settings'),
        do_open=args.get('open'),
    )

    log.debug('opts=%r', opts)
    return opts


def main(wf):
    """Run the workflow."""
    opts = parse_args()

    # Alternate actions
    # ------------------------------------------------------------------
    if opts.do_open:
        return do_open(opts)

    elif opts.do_settings:
        return do_settings()

    elif opts.do_update:
        return do_update()

    # Notify user if update is available
    # ------------------------------------------------------------------
    if wf.update_available:
        wf.add_item('Workflow Update is Available',
                    '↩ or ⇥ to install',
                    autocomplete='workflow:update',
                    valid=False,
                    icon=ICON_UPDATE)

    # Try to search git repos
    # ------------------------------------------------------------------
    search_dirs = wf.settings.get('search_dirs', [])

    # Can't do anything with no directories to search
    if not search_dirs or is_defaults(wf.settings):
        if not search_dirs:
            subtitle = (
                'Add at least one object to `search_dirs` with a real `path`. '
                'Use `reposettings` to edit settings.json.'
            )
        else:
            subtitle = (
                '`search_dirs` still uses the placeholder path `{}` — replace it '
                'with a folder that contains your git repos, save, then run `reposupdate`.'
            ).format(DEFAULT_SEARCH_PATH)
        wf.add_item("You haven't configured any directories to search",
                    subtitle,
                    icon=ICON_WARNING)
        wf.send_feedback()
        return 0

    # Reload repos if settings file has been updated
    if settings_updated():
        log.info('settings were updated. Reloading repos...')
        do_update()

    repos = get_repos(opts)

    # Show appropriate warning/info message if there are no repos to
    # show/search
    # ------------------------------------------------------------------
    if not repos:
        if is_running('update'):
            wf.add_item('Updating list of repos…',
                        'Should be done in a few seconds',
                        icon=ICON_INFO)
            wf.rerun = 0.5
        else:
            wf.add_item('No git repos found',
                        'Check your settings with `reposettings`',
                        icon=ICON_WARNING)
        wf.send_feedback()
        return 0

    # Reload results if `update` is running
    if is_running('update'):
        wf.rerun = 0.5

    return do_search(repos, opts)


if __name__ == '__main__':
    wf = Workflow(default_settings=DEFAULT_SETTINGS,
                  update_settings=UPDATE_SETTINGS,
                  help_url=HELP_URL)
    log = wf.logger
    sys.exit(wf.run(main))
