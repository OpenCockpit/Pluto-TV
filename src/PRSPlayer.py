# Copyright (C) 2026 by xcentaurix

"""Shared VoD/live-stream MoviePlayer for the FAST-channel TV Cockpit plugins
(Pluto TV, Rakuten TV, Samsung TV Plus, ...).

Instantiate directly - no per-plugin subclass needed:

    self.session.open(PRSPlayer, service=reference, sid=sid,
                       resume_points=resumePointsInstance)

*resume_points* is each plugin's own ResumePoints instance (see
PlutoUtils/RakutenTVUtils/SamsungTVUtils) - passed in rather than imported
here since each plugin persists its resume points to its own file.
"""

from Components.ActionMap import ActionMap
from Components.config import config
from Components.ServiceEventTracker import ServiceEventTracker
from Screens.InfoBar import MoviePlayer
from Screens.MessageBox import MessageBox
from Tools import Notifications
from enigma import iPlayableService

from . import _


class PRSPlayer(MoviePlayer):

    ENABLE_RESUME_SUPPORT = False

    def __init__(self, session, service, sid, resume_points):
        self.session = session
        self.mpservice = service
        self.id = sid
        self._resume_points = resume_points
        MoviePlayer.__init__(self, self.session, service, sid)
        self.skinName = ["MoviePlayer"]

        self._event_tracker = ServiceEventTracker(
            screen=self,
            eventmap={
                iPlayableService.evStart: self.__serviceStarted,
            }
        )

        self["actions"] = ActionMap(
            ["MoviePlayerActions", "OkActions"],
            {
                "leavePlayerOnExit": self.leavePlayer,
                "leavePlayer": self.leavePlayer,
                "ok": self.toggleShow,
            }, -3
        )
        self.session.nav.playService(self.mpservice)

    def up(self):
        pass

    def down(self):
        pass

    def doEofInternal(self, _playing):
        self.close()

    def __serviceStarted(self):
        service = self.session.nav.getCurrentService()
        seekable = service.seek()
        last, length = self._resume_points.getResumePoint(self.id)
        if last is None or seekable is None:
            return
        length = seekable.getLength() or (None, 0)
        if (last > 900000) and (not length[1] or (last < length[1] - 900000)):
            self.last = last
            last /= 90000
            Notifications.AddNotificationWithCallback(self.playLastCB, MessageBox, _("Do you want to resume this playback?") + "\n" + (_("Resume position at %s") % f"{int(last / 3600)}:{int(last % 3600 / 60):02d}:{int(last % 60):02d}"), timeout=10, default="yes" in config.usage.on_movie_start.value)

    def playLastCB(self, answer):
        if answer is True and self.last:
            self.doSeek(self.last)
        self.hideAfterResume()

    def leavePlayer(self):
        self.is_closing = True
        self._resume_points.setResumePoint(self.session, self.id)
        self.close()

    def leavePlayerConfirmed(self, answer):
        pass
