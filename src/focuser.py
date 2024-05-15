from typing import List
import logging
from enum import IntFlag, IntEnum, auto
import win32com.client

from common.utils import RepeatTimer, init_log, Component, time_stamp, CanonicalResponse
from common.config import Config
from common.ascom import ascom_run, ascom_driver_info, AscomDispatcher
from PlaneWave import pwi4_client
from mastapi import Mastapi
from dlipower.dlipower.dlipower import SwitchedPowerDevice


class FocuserActivities(IntFlag):
    Idle = 0
    Moving = auto()
    StartingUp = auto()
    ShuttingDown = auto()


class FocusDirection(IntEnum):
    In = auto()
    Out = auto()


class Focuser(Mastapi, Component, SwitchedPowerDevice, AscomDispatcher):
    @property
    def ascom(self) -> win32com.client.Dispatch:
        return self._ascom

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Focuser, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        self.conf = Config().toml['focuser']
        self.logger: logging.Logger = logging.getLogger('mast.unit.focuser')
        init_log(self.logger)
        try:
            self._ascom = win32com.client.Dispatch(self.conf['ascom_driver'])
        except Exception as ex:
            self.logger.exception(ex)
            raise ex

        SwitchedPowerDevice.__init__(self, self.conf)
        Component.__init__(self)

        if not self.is_on():
            self.power_on()

        self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()
        self.connect()

        self.target: int | None = None
        self.lower_limit = 0
        self.upper_limit = 30000
        response = ascom_run(self, 'MaxStep')
        if response.failed:
            self.logger.error(f"could not get MaxStep (failure={response.failure})")
        else:
            self.upper_limit = response.value

        self.known_as_good_position: int | None = \
            self.conf['known_as_good_position'] if 'known_as_good_position' in self.conf \
            else (self.upper_limit / 2) if self.upper_limit \
            else None

        self._was_shut_down = False
        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'focuser-timer-thread'
        self.timer.start()

        self.logger.info('initialized')

    def startup(self):
        """
        :mastapi:
        """
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        self.pw.focuser_enable()
        self._was_shut_down = False
        if self.known_as_good_position is not None:
            self.goto(self.known_as_good_position)
        return CanonicalResponse.ok

    def shutdown(self):
        """
        :mastapi:
        """
        if self.connected:
            self.disconnect()
        self.pw.focuser_disable()
        if self.is_on():
            self.power_off()
        self._was_shut_down = True
        return CanonicalResponse.ok

    def connect(self):
        """
        :mastapi:
        """
        if not self.is_on():
            self.power_on()

        ascom_run(self, 'Connected = True')
        response = ascom_run(self, 'Connected')
        if response.failed:
            self.logger.error(f"could not ASCOM Connected = True (failure={response.failure})")
            self.connected = False
        else:
            self.connected = True
        return CanonicalResponse.ok

    def disconnect(self):
        """
        :mastapi:
        """
        self.connected = False
        return CanonicalResponse.ok

    @property
    def connected(self):
        stat = self.pw.status()
        return stat.focuser.is_connected  # and self.ascom and self.ascom.Connected

    @connected.setter
    def connected(self, value):
        if value:
            self.pw.focuser_enable()
            self.pw.focuser_connect()
        else:
            self.pw.focuser_disconnect()
            self.pw.focuser_disable()

        # if self.ascom:
        #     response = ascom_run(self, f'Connected = {value}', True)
        #     if response.failed:
        #         self.logger.error(f"failed to connect (failure='{response.failure}')")

    @property
    def position(self) -> int:
        """
        :mastapi:
        """
        stat = self.pw.status()
        return round(stat.focuser.position)

    def goto(self, position: int | str):
        """
        Sends the focuser to the specified position

        Parameters
        ----------
        position
            The target position

        :mastapi:
        """
        if not self.is_on():
            self.logger.error('Cannot goto - not-powered')
            return CanonicalResponse(errors='not powered')
        if not self.connected:
            self.logger.error('Cannot goto - not-connected')
            return CanonicalResponse(errors='not connected')

        if isinstance(position, str):
            position = int(position)
        st = self.pw.status()
        if st.focuser.position == position:
            self.logger.info(f"already at {position=}")
        else:
            self.target = position
            self.start_activity(FocuserActivities.Moving)
            self.pw.focuser_goto(position)
        return CanonicalResponse.ok

    def goto_known_as_good_position(self):
        """
        Go to the 'known-as-good' position
        :mastapi:
        """
        self.goto(self.known_as_good_position)
        return CanonicalResponse.ok

    def move(self, amount: int, direction: FocusDirection):
        """
        Move the focuser in or out by the specified amount

        Parameters
        ----------
        amount
            How much to move
        direction
            Either In or Out

        :mastapi:
        """
        current_position = self.position
        if direction == FocusDirection.In:
            target = current_position - amount
            if target < self.lower_limit:
                msg = f"target position ({target}) would be below lower limit ({self.lower_limit})"
                self.logger.error(msg)
                return CanonicalResponse(errors=msg)
        else:
            target = current_position + amount
            if target >= self.upper_limit:
                msg = f"target position ({target}) would be below upper limit ({self.upper_limit})"
                self.logger.error(msg)
                return CanonicalResponse(errors=msg)

        self.goto(position=target)
        return CanonicalResponse.ok

    def abort(self):
        """
        Aborts any in-progress focuser activities

        :mastapi:
        Returns
        -------

        """
        if self.is_active(FocuserActivities.Moving):
            self.pw.focuser_stop()
            self.end_activity(FocuserActivities.Moving)

        if self.is_active(FocuserActivities.StartingUp):
            self.end_activity(FocuserActivities.StartingUp)
        return CanonicalResponse.ok

    def ontimer(self):

        if self.is_active(FocuserActivities.Moving) and self.position == self.target:
            self.end_activity(FocuserActivities.Moving)
            self.target = None

    def status(self) -> dict:
        """

        :mastapi:
        Returns
        -------
            FocuserStatus

        """
        stat = self.pw.status()
        ret = self.power_status() | self.ascom_status() | self.component_status()
        response = ascom_run(self, 'IsMoving')
        is_moving = response.value if response.succeeded else stat.focuser.is_moving
        ret |= {
            'lower_limit': self.lower_limit,
            'upper_limit': self.upper_limit,
            'known_as_good_position': self.known_as_good_position,
            'position': self.position,
            'target': self.target,
            'moving': is_moving,
        }
        time_stamp(ret)
        return ret

    @property
    def name(self) -> str:
        return 'focuser'

    @property
    def operational(self) -> bool:
        st = self.pw.status()
        return all([not self.was_shut_down, self.is_on(), st.focuser.exists, st.focuser.is_connected])

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.is_on():
            ret.append(f"{self.name}: not powered")
        else:
            if self.was_shut_down:
                ret.append(f"{self.name}: shut down")
            if not self.detected:
                ret.append(f"{self.name}: not detected")
            else:
                st = self.pw.status()
                if not st.focuser.exists:
                    ret.append(f"{self.name}: (PWI4) - does not exist")
                elif not st.focuser.is_connected:
                    ret.append(f"{self.name}: (PWI4) - not connected")
        return ret

    @property
    def detected(self) -> bool:
        st = self.pw.status()
        return st.focuser.exists

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down
