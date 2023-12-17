# -*- coding: utf-8 -*-

# University of Potsdam
"""Wordle bot logic including dialog and game phases."""

import logging
import os
import random
import string
from threading import Timer
from time import sleep

import requests
import socketio

from templates import TaskBot
from .dataloader import Dataloader
from .config import (
    COLOR_MESSAGE,
    PLATFORM,
    PROLIFIC_URL,
    PUBLIC,
    SEED,
    STANDARD_COLOR,
    TASK_GREETING,
    TASK_TITLE,
    TIME_LEFT,
    TIME_ROUND,
    TIME_WAITING,
    WARNING_COLOR,
    VALID_WORDS,
    WORDLE_WORDS,
    WORDS_PER_ROOM
)


LOG = logging.getLogger(__name__)


class RoomTimers:
    """A number of timed events during the game.
    :param round_timer: After 15 minutes the image will change
        and players get no points
    """

    def __init__(self):
        self.left_room = dict()
        self.round_timer = None

    def cancel_all_timers(self):
        self.round_timer.cancel()
        for timer in self.left_room.values():
            timer.cancel()

    def user_joined(self, user):
        timer = self.left_room.get(user)
        if timer is not None:
            self.left_room[user].cancel()

    def user_left(self, user):
        self.left_room[user] = Timer(
            LEAVE_TIMER * 60, self.function, args=[self.room_id, "user_left"]
        )
        self.left_room[user].start()

    def start_round_timer(self, function, room_id):
        # cancel old timer if still running
        if isinstance(self.round_timer, Timer):
            self.round_timer.cancel()

        timer = Timer(TIME_ROUND * 60, function, args=[room_id])
        timer.start()
        self.round_timer = timer


class Session:
    def __init__(self):
        self.timer = RoomTimers()
        self.words = Dataloader(WORDLE_WORDS, WORDS_PER_ROOM)
        self.word_to_guess = None
        self.players = list()
        self.guesses = dict()
        self.guesses_history = list()
        self.points = 0
        self.game_over = False

    def close(self):
        self.timer.cancel_all_timers()


class SessionManager(dict):
    def create_session(self, room_id):
        self[room_id] = Session()

    def clear_session(self, room_id):
        if room_id in self:
            self[room_id].close()
            self.pop(room_id)


class WordleBot2 (TaskBot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sessions = SessionManager()
        self.public = PUBLIC
        # maps number of guesses to points
        self.point_system = dict(zip([6, 5, 4, 3, 2, 1], [100, 50, 25, 10, 5, 1]))

        # ensure all the words from the initial image file are guessable
        # with open(DATA_PATH) as infile:
        #     self.wordlist.update(line.split("\t")[0] for line in infile)

        self.waiting_timer = None
        self.received_waiting_token = set()

    def post_init(self, waiting_room, version):
        """save extra variables after the __init__() method has been called"""
        self.waiting_room = waiting_room
        self.version = version

    def on_task_room_creation(self, data):
        """Triggered after a new task room is created."""
        room_id = data["room"]
        task_id = data["task"]

        LOG.debug(f"A new task room was created with id: {data['task']}")
        LOG.debug(f"This bot is looking for task id: {self.task_id}")

        if task_id is not None and task_id == self.task_id:
            for usr in data["users"]:
                self.received_waiting_token.discard(usr["id"])

            # create image items for this room
            LOG.debug("Create data for the new task room...")
            LOG.debug(data)

            self.move_divider(room_id, 20, 80)

            self.sessions.create_session(room_id)

            LOG.debug(self.sessions[room_id].words)
            self.sessions[room_id].players = []
            for usr in data["users"]:
                self.sessions[room_id].players.append(
                    {**usr, "msg_n": 0, "status": "joined"}
                )

            response = requests.post(
                f"{self.uri}/users/{self.user}/rooms/{room_id}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            self.request_feedback(response, "let wordle bot join room")

            logging.info(room_id)

            self.sio.emit(
                "message_command",
                {"command": {"command": "wordle_init"}, "room": room_id},
            )

            # self.show_item(room_id)

            self.sessions[room_id].word_to_guess = self.sessions[room_id].words[0][
                "target_word"].lower()

            # begin timers
            self.sessions[room_id].timer.start_round_timer(
                self.time_out_round, room_id
            )

            # show info to users
            self._update_score_info(room_id)
            self.start_round(room_id)

    def register_callbacks(self):
        @self.sio.event
        def joined_room(data):
            """Triggered once after the bot joins a room."""
            room_id = data["room"]
            if self.version == "clue":
                mode_message = "You will get a clue about the words you need to guess." \
                               " It will appear in the chat area."

                response = requests.patch(
                f"{self.uri}/rooms/{room_id}/text/mode",
                json={"text": mode_message},
                headers={"Authorization": f"Bearer {self.token}"}
                )
                self.request_feedback(response, "add mode explanation")

            if room_id in self.sessions:
                # read out task greeting
                for line in TASK_GREETING:
                    self.sio.emit(
                        "text",
                        {
                            "message": COLOR_MESSAGE.format(
                                color=STANDARD_COLOR, message=line
                            ),
                            "room": room_id,
                            "html": True,
                        },
                    )
                    # sleep(0.5)

                self.sio.emit(
                    "text",
                    {
                        "message": COLOR_MESSAGE.format(
                            color=STANDARD_COLOR,
                            message=f"Let's start with the first "
                        ),
                        "room": room_id,
                        "html": True,
                    },
                )
                sleep(0.5)
                # BUT CAN"T FIND instr_title  IN THE LAYOUT
                response = requests.patch(
                    f"{self.uri}/rooms/{room_id}/text/instr_title",
                    json={"text": line},
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                self.request_feedback(response, "set task instruction title")

        @self.sio.event
        def status(data):
            """Triggered if a user enters or leaves a room."""
            # check whether the user is eligible to join this task
            task = requests.get(
                f"{self.uri}/users/{data['user']['id']}/task",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            self.request_feedback(task, "get task")
            if not task.json() or task.json()["id"] != int(self.task_id):
                return

            room_id = data["room"]
            # someone joined waiting room
            if room_id == self.waiting_room:
                if self.waiting_timer is not None:
                    LOG.debug("Waiting Timer stopped.")
                    self.waiting_timer.cancel()
                if data["type"] == "join":
                    LOG.debug("Waiting Timer restarted.")
                    self.waiting_timer = Timer(
                        TIME_WAITING * 60,
                        self._no_partner,
                        args=[room_id, data["user"]["id"]],
                        )
                    self.waiting_timer.start()
                    sleep(10)
                    self.sio.emit(
                        "text",
                        {
                            "message": COLOR_MESSAGE.format(
                                color=STANDARD_COLOR,
                                message=f"If nobody shows up within "
                                        f"{TIME_WAITING} minutes, I will give "
                                        f"you a submission link, so that you "
                                        f"can get paid for your waiting time."
                            ),
                            "room": room_id,
                            "receiver_id": data['user']['id'],
                            "html": True,
                        },
                    )

        # some joined a task room
                if data["type"] == "join":
                    # inform everyone about the join event
                    self.send_message_to_user(
                        f"{data['user']['name']} has joined the game.", room_id
                    )

                            # # cancel timer
                            # LOG.debug(
                            #     f"Cancelling Timer: left room for user {curr_usr['name']}"
                            # )
                            # self.sessions[room_id].timer.user_joined(curr_usr["id"])

                elif data["type"] == "leave":
                    self.send_message_to_user(f"{data['user']['name']} has left the game.", room_id)

        @self.sio.event
        def text_message(data):
            """Triggered once a text message is sent (no leading /).

            Count user text messages.
            If encountering something that looks like a command
            then pass it on to be parsed as such.
            """
            LOG.debug(f"Received a message from {data['user']['name']}.")
            room_id = data["room"]
            user_id = data["user"]["id"]

            # filter irrelevant messages
            if room_id not in self.sessions or user_id == self.user:
                return

            # if the message is part of the main discussion count it
            for usr in self.sessions[room_id].players:
                if usr["id"] == user_id and usr["status"] == "ready":
                    usr["msg_n"] += 1

        @self.sio.event
        def command(data):
            """Parse user commands."""
            LOG.debug(
                f"Received a command from {data['user']['name']}: {data['command']}"
            )

            room_id = data["room"]
            user_id = data["user"]["id"]

            # do not process commands from itself
            if str(user_id) == self.user:
                return

            if room_id in self.sessions:
                # only accept commands from the javascript
                # frontend (commands are dictionaries)
                if isinstance(data["command"], dict):
                    if "guess" in data["command"]:
                        if data["command"]["guess"].strip() == "":
                            self.sio.emit(
                                "text",
                                {
                                    "message": COLOR_MESSAGE.format(
                                        color=WARNING_COLOR,
                                        message="**You need to provide a guess!**",
                                    ),
                                    "room": room_id,
                                    "receiver_id": user_id,
                                    "html": True,
                                },
                            )
                        else:
                            self._command_guess(room_id, user_id, data["command"])

                # bot has no user defined commands
                else:
                    self.sio.emit(
                        "text",
                        {
                            "message": COLOR_MESSAGE.format(
                                color=STANDARD_COLOR,
                                message="Sorry, but I do not understand this command.",
                            ),
                            "room": room_id,
                            "receiver_id": user_id,
                            "html": True,
                        },
                    )

    def move_divider(self, room_id, chat_area=50, task_area=50):
        """move the central divider and resize chat and task area
        the sum of char_area and task_area must sum up to 100
        """
        if chat_area + task_area != 100:
            LOG.error("Could not resize chat and task area: invalid parameters.")
            raise ValueError("chat_area and task_area must sum up to 100")

        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/sidebar",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"attribute": "style", "value": f"width: {task_area}%"},
        )
        self.request_feedback(response, "resize sidebar")

        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/content",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"attribute": "style", "value": f"width: {chat_area}%"},
        )
        self.request_feedback(response, "resize content area")

    def _command_guess(self, room_id, user_id, command):
        """Must be sent to end a game round."""

        LOG.debug(command)

        # get the wordle for this room and the guess from the user
        word = self.sessions[room_id].word_to_guess

        guess = command["guess"]
        remaining_guesses = command["remaining"]

        # make sure the guess has the right length
        if len(word ) != len(guess):
            self.sio.emit(
                "text",
                {
                    "message": COLOR_MESSAGE.format(
                        color=STANDARD_COLOR,
                        message=f"Unfortunately this word is not valid. "
                        f"Your guess needs to have {len(word)} letters.",
                    ),
                    "receiver_id": user_id,
                    "room": room_id,
                    "html": True,
                },
            )
            self.sio.emit(
                "message_command",
                {
                    "command": {"command": "unsubmit"},
                    "room": room_id,
                    "receiver_id": user_id,
                },
            )
            return

        # make sure it's a good guess
        if guess not in VALID_WORDS:
            self.sio.emit(
                "text",
                {
                    "message": COLOR_MESSAGE.format(
                        color=WARNING_COLOR,
                        message="**Unfortunately this word is not valid. "
                        "Make sure that there aren't any typos**",
                    ),
                    "receiver_id": user_id,
                    "room": room_id,
                    "html": True,
                },
            )
            self.sio.emit(
                "message_command",
                {
                    "command": {"command": "unsubmit"},
                    "room": room_id,
                    "receiver_id": user_id,
                },
            )
            return

        self.sessions[room_id].guesses = dict()
        self.sessions[room_id].guesses_history.append(guess)
        self.sio.emit(
            "message_command",
            {
                "command": {
                    "command": "wordle_guess",
                    "guess": guess,
                    "correct_word": word,
                },
                "room": room_id,
            },
        )

        if (word == guess) or (remaining_guesses == 1):
            sleep(2)

            result = "LOST"
            points = 0

            if word == guess:
                result = "WON"
                points = self.point_system[int(remaining_guesses)]

            # update points for this room
            self.sessions[room_id].points += points

            # self.timers_per_room[room_id].done_timer.cancel()
            self.sio.emit(
                "text",
                {
                    "message": COLOR_MESSAGE.format(
                        color=STANDARD_COLOR,
                        message=(
                            f"**YOU {result}! For this round you get {points} points. "
                            f"Your total score is: {self.sessions[room_id].points}**"
                        ),
                    ),
                    "room": room_id,
                    "html": True,
                },
            )

            self.load_next_game(room_id)

    def _update_score_info(self, room):
        response = requests.patch(
            f"{self.uri}/rooms/{room}/text/subtitle",
            json={
                "text": f"Your score is {self.sessions[room].points} – You have {len(self.sessions[room].words)} rounds to go."
            },
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.request_feedback(response, "update score")

    def load_next_game(self, room_id):
        """
        Load the next image and wordle and move to the next round if possible
        """
        self.sessions[room_id].words.pop(0)
        self.sessions[room_id].guesses_history = list()
        self.sessions[room_id].guesses = dict()

        # was this the last game round?
        if not self.sessions[room_id].words:
            self.sio.emit(
                "text",
                {
                    "message": COLOR_MESSAGE.format(
                        color=STANDARD_COLOR,
                        message="The game is over! Thank you for participating!",
                    ),
                    "room": room_id,
                    "html": True,
                },
            )
            self._update_score_info(room_id)
            sleep(1)

            # close the game, bot users get a success token
            self.sessions[room_id].game_over = True
            self.end_game(
                room_id, {self.sessions[room_id].players[0]["id"]: "success"}
            )
            sleep(1)
            self.close_room(room_id)
        else:
            # load the next word
            self.start_round(room_id)

    def start_round(self, room_id):
        if not self.sessions[room_id].words:
            self.close_room(room_id)
        else:
            self.sessions[room_id].word_to_guess = self.sessions[room_id].words[0][
                "target_word"].lower()

            self.sio.emit(
                "text",
                {
                    "message": COLOR_MESSAGE.format(
                        color=STANDARD_COLOR,
                        message=f"Ok, let's move on to the next round. "
                                f"{len(self.sessions[room_id].words)} rounds to go!",
                    ),
                    "room": room_id,
                    "html": True,
                },
            )
            self.send_message_to_user(f"{self.sessions[room_id].word_to_guess}", room_id)
            if self.version == "clue":
                self.send_message_to_user(f"CLUE: {self.sessions[room_id].words[0]['target_word_clue'].lower()}",
                                          room_id)

            self._update_score_info(room_id)
            sleep(2)
            self.sio.emit(
                "message_command",
                {"command": {"command": "wordle_init"}, "room": room_id},
            )

            # reset attributes for the new round
            for usr in self.sessions[room_id].players:
                usr["status"] = "ready"
                usr["msg_n"] = 0

            # self.show_item(room_id)

            # restart next_round_timer
            self.sessions[room_id].timer.start_round_timer(self.time_out_round, room_id)

    def time_out_round(self, room_id):
        """
        function called by the round timer once the time is over.
        Inform the users that the time is up and move to the next round
        """
        self.sio.emit(
            "text",
            {
                "message": COLOR_MESSAGE.format(
                    color=WARNING_COLOR,
                    message="**Your time is up! Unfortunately you get no points for this round.**",
                ),
                "room": room_id,
                "html": True,
            },
        )
        self.load_next_game(room_id)

    def _no_partner(self, room_id, user_id):
        """Handle the situation that a participant waits in vain."""
        if user_id not in self.received_waiting_token:
            self.sio.emit(
                "text",
                {"message": "Unfortunately we could not find a partner for you!",
                 "room": room_id,
                 "receiver_id": user_id,
                },
            )
            # create token and send it to user
            self.confirmation_code(room_id, "no_partner", receiver_id=user_id)
            sleep(5)
            self.sio.emit(
                "text",
                {
                    "message": "You may also wait some more :)",
                    "room": room_id,
                    "receiver_id": user_id,
                },
            )
            # no need to cancel
            # the running out of this timer triggered this event
            self.waiting_timer = Timer(
                TIME_WAITING * 60, self._no_partner, args=[room_id, user_id]
            )
            self.waiting_timer.start()
            self.received_waiting_token.add(user_id)
        else:
            self.sio.emit(
                "text",
                {"message": "You won't be remunerated for further waiting time.",
                 "room": room_id,
                 "receiver_id": user_id,
                },
            )

    def send_message_to_user(self, message, room, receiver=None):
        if receiver:
            self.sio.emit(
                "text",
                {"message": f"{message}", "room": room, "receiver_id": receiver},
            )
        else:
            self.sio.emit(
                "text",
                {"message": f"{message}", "room": room},
            )

    def confirmation_code(self, room_id, status, receiver_id=None):
        """Generate AMT token that will be sent to each player."""
        kwargs = dict()
        # either only for one user or for both
        if receiver_id is not None:
            kwargs["receiver_id"] = receiver_id

        confirmation_token = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        # post confirmation token to logs
        response = requests.post(
            f"{self.uri}/logs",
            json={
                "event": "confirmation_log",
                "room_id": room_id,
                "data": {"status_txt": status, "confirmation_token": confirmation_token},
                **kwargs,
            },
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.request_feedback(response, "post confirmation token to logs")

        if self.data_collection == "AMT":
            self._show_amt_token(room_id, receiver_id, confirmation_token)
        elif self.data_collection == "Prolific":
            self._show_prolific_link(room_id, receiver_id)

        return confirmation_token

    def _show_prolific_link(self, room, receiver, token=None):

        if token is None:
            # use the username
            response = requests.get(
                f"{self.uri}/users/{receiver}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            self.request_feedback(response, "get user")
            token = response.json().get("name", f"{room}–{receiver}")

        url = f"{PROLIFIC_URL}{token}"
        self.sio.emit(
            "text",
            {"message": f"Please return to <a href='{url}'>{url}</a> to complete your submission.",
             "room": room,
             "html": True,
             "receiver_id": receiver
             }
        )

    def _show_amt_token(self, room, receiver, token):
        self.sio.emit(
            "text",
            {
                "message": COLOR_MESSAGE.format(
                    color=STANDARD_COLOR,
                    message="Please enter the following token into "
                            "the field on the HIT webpage, and close "
                            "this browser window.",
                ),
                "room": room,
                "html": True,
                "receiver_id": receiver
            },
        )
        self.sio.emit(
            "text",
            {
                "message": COLOR_MESSAGE.format(
                    color=STANDARD_COLOR, message=f"Here's your token: {token}"
                ),
                "room": room,
                "html": True,
                "receiver_id": receiver
            },
        )

        # TODO: show token also in display area

    def social_media_post(self, room_id, this_user_id, other_user_name):
        self.sio.emit(
            "text",
            {
                "message": COLOR_MESSAGE.format(
                    color=STANDARD_COLOR,
                    message=(
                        "Please share the following text on social media: "
                        "I played slurdle and helped science! "
                        f"Together with {other_user_name}, "
                        f"I got {self.sessions[room_id].points} "
                        f"points for {self.sessions[room_id].words.n} puzzle(s). "
                        f"Play here: {self.uri}. #slurdle"
                    ),
                ),
                "receiver_id": this_user_id,
                "room": room_id,
                "html": True,
            },
        )

    def end_game(self, room_id, user_dict):
        # if self.public:
        #     self.social_media_post(room_id, self.sessions[room_id].players[0]["id"])
        # else:
        for user_id, status in user_dict.items():
            self.confirmation_code(room_id, status, user_id)
            sleep(0.5)

    def close_room(self, room_id):
        self.sio.emit(
            "text",
            {
                "message": COLOR_MESSAGE.format(
                    color=STANDARD_COLOR,
                    message="This room is closing now.",
                ),
                "room": room_id,
                "html": True,
            },
        )

        self.room_to_read_only(room_id)

        # remove any task room specific objects
        self.sessions.clear_session(room_id)

    def room_to_read_only(self, room_id):
        """Set room to read only."""
        # set room to read-only
        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/text",
            json={"attribute": "readonly", "value": "True"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.request_feedback(response, "set room to read_only")

        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/text",
            json={"attribute": "placeholder", "value": "This room is read-only"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.request_feedback(response, "set room to read_only")

        # remove user from room
        if room_id in self.sessions:
            for usr in self.sessions[room_id].players:
                response = requests.get(
                    f"{self.uri}/users/{usr['id']}",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                self.request_feedback(response, "get user")
                etag = response.headers["ETag"]

                response = requests.delete(
                    f"{self.uri}/users/{usr['id']}/rooms/{room_id}",
                    headers={"If-Match": etag, "Authorization": f"Bearer {self.token}"},
                )
                self.request_feedback(response, "remove user from task room")