Google Calendar sync
====================
khal can sync with Google Calendar on its own, without vdirsyncer or any other
tool. Synchronization is two-way: events you create, edit or delete in khal are
pushed to Google, and changes made in Google are pulled into khal. All
calendars of the account are synced, including ones you created or that were
shared with you.

Under the hood khal talks to Google's CalDAV endpoint and authenticates with
OAuth2 (loopback redirect with PKCE). Only the python standard library is used.

Google side setup
-----------------
This has to be done once, and needs your own OAuth client:

1. Create a project in the `Google Cloud Console`_.
2. Enable the **Google Calendar API** and the **CalDAV API** for that project.
   Without the CalDAV API, syncing fails with ``accessNotConfigured``.
3. Configure the OAuth consent screen and add your Google account as a *test
   user*.
4. Create an *OAuth client ID* of type **Desktop app** and note the client id
   and secret.

.. _Google Cloud Console: https://console.cloud.google.com/

khal side setup
---------------
Add a ``[sync]`` section and a calendar that discovers the synced directories
(khal creates one subdirectory per Google calendar):

.. code-block:: ini

    [sync]
    client_id = <your client id>
    client_secret = <your client secret>
    calendar_ids = all
    path = ~/.local/share/khal/calendars/google

    [calendars]
    [[google]]
    path = ~/.local/share/khal/calendars/google/*
    type = discover

All options are described in the *sync* section of :doc:`configure`. Then
authorize khal once and sync::

    khal sync --login
    khal sync

``khal sync --login`` opens your browser (use ``--no-browser`` to only print the
URL). The refresh token is stored in ``token_path`` (by default
:file:`~/.local/share/khal/google_token.json`, readable only by you).

Choosing calendars
------------------
``calendar_ids = all`` syncs every calendar of the account. To sync only some,
list their ids, separated by commas. The id of your main calendar is your
e-mail address, the ids of other calendars are shown in their settings in Google
Calendar. Calendar names and colors are taken from Google. Calendars you only
have read access to (for example holidays) are only pulled, never pushed.

Syncing automatically
---------------------
Set ``auto_sync = True`` in the ``[default]`` section to sync before every khal
command (except ``sync`` and ``configure``). This only happens if a quick
connectivity check succeeds, is limited by ``sync_timeout`` seconds, and a
failing sync only prints a warning, khal then continues with the local data.
``khal sync`` itself exits with status 1 on failure, including when offline.

Conflicts
---------
If an event was changed on both sides since the last sync, the version from
Google wins. Your local version is kept next to it as ``<name>.ics.conflict``
(khal ignores this file), so nothing is lost.

Troubleshooting
---------------
``accessNotConfigured``
    The CalDAV API is not enabled in your Google Cloud project, see above. It
    can take a few minutes until the change takes effect.
``Not logged in``
    Run ``khal sync --login``.
``Google returned no refresh token``
    Remove khal's access at https://myaccount.google.com/permissions and run
    ``khal sync --login`` again.
Many ``Event start time and end time are the same`` warnings
    Harmless, some events (e.g. from imported calendars) have no duration. They
    are only logged while khal builds its cache; ``khal -v error ...`` hides
    them.
Switching to a new layout or starting over
    Delete the ``*.ics`` files and :file:`.khal_gsync_state.json` in the synced
    directories (or the whole ``path`` directory) and run ``khal sync`` again.
    Never do this while you have local changes that were not synced yet.
