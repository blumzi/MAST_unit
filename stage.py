
import logging
from enum import Enum, Flag
import datetime
from utils import RepeatTimer, return_with_status, Activities, init_log
from typing import TypeAlias
from mastapi import Mastapi
from power import Power, PowerState, SocketId

logger = logging.getLogger('mast.unit.stage')
init_log(logger)

StageStateType: TypeAlias = "StageState"


class StageActivities(Flag):
    Idle = 0
    Moving = (1 << 0)
    StaringUp = (1 << 1)
    ShuttingDown = (1 << 2)


class StageStatus:
    is_powered: bool
    is_connected: bool
    is_operational: bool
    position: int
    state: StageStateType
    state_verbal: str
    activities: StageActivities
    activities_verbal: str
    api_methods: list
    reasons: list[str]


stage_state_str2int_dict: dict = {

    'Idle': 0,
    'In': 1,
    'Out': 2,
    'MovingIn': 3,
    'MovingOut': 4,
    'Error': 5,
    'Science': 1,
    'Guiding': 2,
}


class StageState(Enum):
    Idle = 0
    In = 1
    Out = 2
    MovingIn = 3
    MovingOut = 4
    Error = 5
    Science = In
    Guiding = Out


class Stage(Mastapi, Activities):

    MIN_TICKS = 0
    MAX_TICKS = 50000
    TICKS_WHEN_IN = 100
    TICKS_WHEN_OUT = 30000
    TICKS_PER_SECOND = 1000

    _connected: bool
    _position: int = 0
    state: StageState
    default_initial_state: StageState = StageState.In
    ticks_at_start: int
    ticks_at_target: int
    motion_start_time: datetime
    timer: RepeatTimer
    activities: StageActivities

    def __init__(self):
        self.state = StageState.Idle
        self._connected = False

        self.timer = RepeatTimer(1, function=self.ontimer)
        self.timer.name = 'mast.stage'
        self.timer.start()
        self.activities = StageActivities.Idle
        logger.info('initialized')

    @property
    def is_powered(self):
        return Power.is_on(SocketId('Stage'))

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value):
        if not self.is_powered:
            return

        if value:
            try:
                # connect to the controller
                # get the stage location
                # if it is not at the preferred initial stage location (In/Out?)
                #  - move it
                #  - set self.state accordingly
                self.state = StageState.In
                self.position = self.TICKS_WHEN_IN

            except Exception as ex:
                logger.exception(ex)
                self.state = StageState.Error
                raise ex

        self._connected = value
        logger.info(f'connected = {value}')

    @return_with_status
    def connect(self):
        """
        Connects to the **MAST** stage controller

        :mastapi:
        """
        if self.is_powered:
            self.connected = True

    @return_with_status
    def disconnect(self):
        """
        Disconnects from the **MAST** stage controller

        :mastapi:
        """
        if self.is_powered:
            self.connected = False

    @return_with_status
    def startup(self):
        """
        Startup routine for the **MAST** stage.  Makes it ``operational``:
        * If not powered, powers it ON
        * If not connected, connects to the controller
        * If the stage is not at operational position, it is moved

        :mastapi:
        """
        if not self.is_powered:
            Power.power(SocketId('Stage'), PowerState.On)
        if not self.connected:
            self.connect()
        if self.state is not StageState.Science:
            self.start_activity(StageActivities.StaringUp, logger)
            self.move(StageState.Science)

    @return_with_status
    def shutdown(self):
        """
        Shutdown routine for the **MAST** stage.  Makes it ``idle``

        :mastapi:
        """
        if not self.is_powered:
            return

        if not self.state == StageState.Guiding:
            self.start_activity(StageActivities.ShuttingDown, logger)
            self.move(StageState.Guiding)
        self.disconnect()
        Power.power(SocketId('Stage'), PowerState.Off)

    @property
    def position(self) -> int:
        return self._position

    @position.setter
    def position(self, value):
        if self.connected:
            self._position = value

    def status(self) -> StageStatus:
        """
        Returns the status of the MAST stage
        :mastapi:
        """
        st = StageStatus()
        st.reasons = list()
        st.is_powered = self.is_powered
        if st.is_powered:
            st.is_connected = self.connected
            if st.is_connected:
                st.state = self.state
                st.is_operational = st.state == StageState.Science
                if not st.is_operational:
                    st.reasons.append(f'state is {st.state} instead of {StageState.Science}')
                st.state_verbal = st.state.name
                st.position = self.position
                st.activities = self.activities
                st.activities_verbal = st.activities.name
            else:
                st.reasons.append('not-connected')
        else:
            st.is_powered = False
            st.is_operational = False
            st.is_connected = False
            st.reasons.append('not-powered')
            st.reasons.append('not-connected')
        return st

    def ontimer(self):
        if not self.connected:
            return

        if self.is_active(StageActivities.Moving):
            dt = (datetime.datetime.now() - self.motion_start_time).seconds
            if self.state == StageState.MovingOut:
                pos = self.ticks_at_start + dt * self.TICKS_PER_SECOND
                if pos > self.TICKS_WHEN_OUT:
                    pos = self.TICKS_WHEN_OUT
                    self.state = StageState.Out
                    self.end_activity(StageActivities.Moving, logger)
            else:
                pos = self.ticks_at_start - dt * self.TICKS_PER_SECOND
                if pos <= self.TICKS_WHEN_IN:
                    pos = self.TICKS_WHEN_IN
                    self.state = StageState.In
                    self.end_activity(StageActivities.Moving, logger)

            self.position = pos
            if self.is_active(StageActivities.StaringUp) and self.state == StageState.In:
                self.end_activity(StageActivities.StaringUp, logger)
            if self.is_active(StageActivities.ShuttingDown) and self.state == StageState.Out:
                self.end_activity(StageActivities.ShuttingDown, logger)
            logger.info(f'ontimer: position={self.position}')

    @return_with_status
    def move(self, where: StageState | str):
        """
        Starts moving the stage to one of two pre-defined positions
        :mastapi:
        :param where: Where to move the stage to (either StageState.Science or StageState.Guiding)
        """
        if not self.connected:
            return

        if isinstance(where, str):
            where = StageState(stage_state_str2int_dict[where])
        if self.state == where:
            logger.info(f'move: already {where}')
            return

        self.start_activity(StageActivities.Moving, logger)
        self.state = StageState.MovingIn if where == StageState.In else StageState.MovingOut
        self.ticks_at_start = self.position
        self.motion_start_time = datetime.datetime.now()
        logger.info(f'move: at {self.position} started moving, state={self.state}')
