import logging
import os

from time import sleep


import requests
from templates import TaskBot

import logging

from wordle_words.config import GUESSER_PROMPT,  WORDLE_WORDS, WORDS_PER_ROOM,  LETTER_FEEDBACK_EXAMPLE
from wordle_words.dataloader import Dataloader


LOG = logging.getLogger(__name__)

class Session:
    def __init__(self):
        self.players = list()
        self.words = Dataloader(WORDLE_WORDS, WORDS_PER_ROOM)
        self.word_to_guess = None
        self.word_letters = {}
        self.guesses = 0
        self.guesser = None
        self.points = {
            "score": 0,
            "history": [{"correct": 0, "wrong": 0, "warnings": 0}],
        }

    def close(self):
        pass

class SessionManager(dict):
    def create_session(self, room_id):
        self[room_id] = Session()

    def clear_session(self, room_id):
        if room_id in self:
            self[room_id].close()
            self.pop(room_id)

class WordleBot(TaskBot):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received_waiting_token = set()
        self.sessions = SessionManager()
        self.register_callbacks()

    def post_init(self, waiting_room, version):
        """
        save extra variables after the __init__() method has been called
        and create the init_base_dict: a dictionary containing
        needed arguments for the init event to send to the JS frontend
        """
        self.waiting_room = waiting_room
        self.version = version


    def on_task_room_creation(self, data):
        """This function is executed as soon as 2 users are paired and a new
        task took is created
        """
        room_id = data["room"]

        task_id = data["task"]
        logging.debug(f"A new task room was created with id: {data['room']}")
        logging.debug(f"A new task room was created with task id: {data['task']}")
        logging.debug(f"This bot is looking for task id: {self.task_id}")

        self.log_event("bot_version_log", {"version": self.version}, room_id)

        if task_id is not None and task_id == self.task_id:
            # modify layout
            for usr in data["users"]:
                self.received_waiting_token.discard(usr["id"])
            logging.debug("Create data for the new task room...")
            # create a new session for these users
            # this_session = self.sessions[room_id]
            self.sessions.create_session(room_id)

            for usr in data["users"]:
                self.sessions[room_id].players.append(
                    {**usr, "msg_n": 0, "status": "joined"}
                )
            #     only one player, do wee need players/guesser?/
            self.sessions[room_id].guesser = data["users"][0]["id"]

            # join the newly created room
            response = requests.post(
                f"{self.uri}/users/{self.user}/rooms/{room_id}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
        self.send_instructions(room_id)
        self.sio.emit(
            "text",
            {
                "message": (
                    "Are you ready? <br>"
                    "<button class='message_button' onclick=\"confirm_ready('yes')\">YES</button> "
                    "<button class='message_button' onclick=\"confirm_ready('no')\">NO</button>"
                ),
                "room": room_id,
                # "receiver_id": player["id"],
                "html": True,
            },
        )

    @staticmethod
    def message_callback(success, error_msg="Unknown Error"):
        if not success:
            LOG.error(f"Could not send message: {error_msg}")
            exit(1)
        LOG.debug("Sent message successfully.")

    def register_callbacks(self):
        @self.sio.event
        def user_message(data):
            LOG.debug("Received a user_message.")
            LOG.debug(data)
            # it sends to itself, user id = null, receiver if = null
            self.sio.emit("text", {"message": data["message"], "room": data["room"]})

        @self.sio.event
        def status(data):
            """Triggered when a user enters or leaves a room."""
            room_id = data["room"]
            event = data["type"]
            user = data["user"]

            # don't do this for the bot itself
            if user["id"] == self.user:
                return

            # someone joined a task room
            if event == "join":
                # inform everyone about the join event
                self.send_message_to_user(
                    f"{user['name']} has joined the game.", room_id
                )
                sleep(0.5)

            elif event == "leave":
                self.send_message_to_user(f"{user['name']} has left the game.", room_id)

                # # remove this user from current session
                # this_session.players = list(
                #     filter(
                #         lambda player: player["id"] != user["id"], this_session.players
                #     )
                # )
                #



        @self.sio.event
        def text_message(data):
            """Parse user messages."""
            LOG.debug(
                f"Received a message from {data['user']['name']}: {data['message']}"
            )
            room_id = data["room"]
            user_id = data["user"]["id"]

            if user_id == self.user:
                return

            this_session = self.sessions[room_id]
            self.log_event("guess", {"content": data["message"]}, room_id)
            if not valid_guess(data["message"]):
                self.log_event("invalid guess", {"content": data["message"]}, room_id)
                self.send_message_to_user(
                    f"The guess must be a single 5-letter word! You lost this round.",
                    room_id,
                )
                self.update_reward(room_id, 0)
                self.load_next_game(room_id)
                return

            letter_feedback = check_guess(data["message"].lower(), this_session)
            if letter_feedback == "correct":
                this_session.guesses += 1
                self.send_message_to_user(f"You guessed the word!", room_id)
                self.update_reward(room_id, 1)
                self.load_next_game(room_id)
                return

            if this_session.guesses < 5:
                this_session.guesses += 1
                html_feedback, text_feedback = format_string(letter_feedback)
                self.sio.emit(
                    "text",
                    {
                        "message": f'{html_feedback} ({text_feedback})',
                        "room": room_id,
                        "html": True,
                    },
                )
                self.send_message_to_user(f"Make a new guess", room_id)

            elif this_session.guesses == 5:
                this_session.guesses += 1
                self.send_message_to_user(
                    f"6 guesses have been already used. You lost this round.",
                    room_id,
                )
                self.update_reward(room_id, 0)
                self.load_next_game(room_id)


        @self.sio.event
        def command(data):
            """Parse user commands."""

            # do not prcess commands from itself
            if data["user"]["id"] == self.user:
                return

            logging.debug(
                f"Received a command from {data['user']['name']}: {data['command']}"
            )

            if isinstance(data["command"], dict):
                # commands from interface
                event = data["command"]["event"]
                if event == "confirm_ready":
                    if data["command"]["answer"] == "yes":
                        self._command_ready(data["room"], data["user"])
                    elif data["command"]["answer"] == "no":
                        self.send_message_to_user(
                            "OK, read the instructions carefully and click on <yes> once you are ready.",
                            data["room"],
                            data["user"]["id"],
                        )

    def _command_ready(self, room_id, user):
        """Must be sent to begin a conversation."""

        # the user has sent /ready repetitively
        if self.sessions[room_id].players[0]["status"] in {"ready", "done"}:
            self.send_message_to_user(
                "You have already typed 'ready'.", room_id, user["id"]
            )
            return
        self.sessions[room_id].players[0]["status"] = "ready"
        self.send_message_to_user("Woo-Hoo! The game will begin now.", room_id)
        self.start_round(room_id)

    def start_round(self, room_id):
        if not self.sessions[room_id].words:
            self.close_room(room_id)

        # send the instructions for the round
        round_n = (WORDS_PER_ROOM - len(self.sessions[room_id].words)) + 1

        self.log_event("round", {"number": round_n}, room_id)
        self.send_message_to_user(f"Let's start round {round_n}", room_id)
        self.sessions[room_id].word_to_guess = self.sessions[room_id].words[0][
            "target_word"].lower()
        self.sessions[room_id].guesses = 0
        self.send_message_to_user(f"(The word to guess is {self.sessions[room_id].word_to_guess})", room_id)
        self.sessions[room_id].word_letters = decompose(self.sessions[room_id].word_to_guess)
        if self.version == "clue":
            self.send_message_to_user(
                f'CLUE for the word: {self.sessions[room_id].words[0]["target_word_clue"].lower()} ',
                room_id,
                self.sessions[room_id].guesser,
            )
        self.send_message_to_user(
            "Make your guess:",
            room_id,
            self.sessions[room_id].guesser,
        )

    def load_next_game(self, room_id):
        # word list gets smaller, next round starts
        self.sessions[room_id].words.pop(0)
        if not self.sessions[room_id].words:
            self.close_room(room_id)
            return
        self.start_round(room_id)

    def send_instructions(self, room_id):
        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/text/instr",
            json={"text": f"{GUESSER_PROMPT}", "receiver_id": self.sessions[room_id].guesser},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if not response.ok:
            LOG.error(f"Could not set task instruction: {response.status_code}")
            response.raise_for_status()

        self.sio.emit(
            "message_command",
            {
                "command": {
                    "event": "color_text",
                    "message": f'{LETTER_FEEDBACK_EXAMPLE}'
                },
                "room": room_id,
                "receiver_id": self.sessions[room_id].guesser,
            }
        )

    def update_reward(self, room_id, reward):
        score = self.sessions[room_id].points["score"]
        score += reward
        score = round(score, 2)
        self.sessions[room_id].points["score"] = max(0, score)
        self.update_title_points(room_id, reward)

    def update_title_points(self, room_id, reward):
        score = self.sessions[room_id].points["score"]
        correct = self.sessions[room_id].points["history"][0]["correct"]
        wrong = self.sessions[room_id].points["history"][0]["wrong"]
        if reward == 0:
            wrong += 1
        elif reward == 1:
            correct += 1

        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/text/title",
            json={
                "text": f"Score: {score} 🏆 | Correct: {correct} ✅ | Wrong: {wrong} ❌"
            },
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.sessions[room_id].points["history"][0]["correct"] = correct
        self.sessions[room_id].points["history"][0]["wrong"] = wrong

        self.request_feedback(response, "setting point stand in title")

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
        # sleep(1)

    def room_to_read_only(self, room_id):
        """Set room to read only."""
        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/text",
            json={"attribute": "readonly", "value": "True"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if not response.ok:
            logging.error(f"Could not set room to read_only: {response.status_code}")
            response.raise_for_status()
        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/text",
            json={"attribute": "placeholder", "value": "This room is read-only"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if not response.ok:
            logging.error(f"Could not set room to read_only: {response.status_code}")
            response.raise_for_status()

        # remove user from room
        if room_id in self.sessions:
            for usr in self.sessions[room_id].players:
                response = requests.get(
                    f"{self.uri}/users/{usr['id']}",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                if not response.ok:
                    logging.error(f"Could not get user: {response.status_code}")
                    response.raise_for_status()
                etag = response.headers["ETag"]

                response = requests.delete(
                    f"{self.uri}/users/{usr['id']}/rooms/{room_id}",
                    headers={"If-Match": etag, "Authorization": f"Bearer {self.token}"},
                )
                if not response.ok:
                    logging.error(
                        f"Could not remove user from task room: {response.status_code}"
                    )
                    response.raise_for_status()
                logging.debug("Removing user from task room was successful.")

    def close_room(self, room_id):
        self.room_to_read_only(room_id)

        # remove any task room specific objects
        self.sessions.clear_session(room_id)


def decompose(word):
    letters = {}
    for i in range (len(word)):
        if word[i] in letters:
            letters[word[i]].append(i)
        else:
            letters[word[i]] = [i]
    return letters


def valid_guess(guess):
    return len(guess.split()) == 1 and len(guess) == 5


def check_guess(guess, session):
    if session.word_to_guess == guess:
        return "correct"
    feedback = []
    for i in range(len(guess)):
        if guess[i] == session.word_to_guess[i]:
            feedback.append((guess[i], "green"))
        else:
            if guess[i] in session.word_letters:
                feedback.append((guess[i], "yellow"))
            else:
                feedback.append((guess[i], "red"))
    return feedback


def format_string(letter_feedback):
    colour_string = ""
    text_string = ""
    for pair in letter_feedback:
        colour_string += f'<a style = "color:{pair[1]};" > <b> {pair[0].upper()}  </b> </a>'
        text_string += f'{pair[0].upper()}  < {pair[1]}> '
    return colour_string, text_string


if __name__ == "__main__":
    # set up logging configuration
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s:%(message)s")

    # create commandline parser
    parser = WordleBot.create_argparser()


    if "WAITING_ROOM" in os.environ:
        waiting_room = {"default": os.environ["WAITING_ROOM"]}
    else:
        waiting_room = {"required": True}
    parser.add_argument(
        "--waiting_room",
        type=int,
        help="room where users await their partner",
        **waiting_room,
    )

    # versions:
    #  clue : a guesser gets a clue about the word before they start guesing
    #  standard: no clue is provided

    if "BOT_VERSION" in os.environ:
        bot_version = {"default": os.environ["BOT_VERSION"]}
    else:
        bot_version = {"required": True}
    parser.add_argument(
        "--bot_version",
        type=str,
        help="version of wordle game",
        **bot_version,
    )

    args = parser.parse_args()
    logging.debug(args)

    # create bot instance
    bot = WordleBot(args.token, args.user, args.task, args.host, args.port)

    bot.post_init(args.waiting_room, args.bot_version)

    # connect to chat server
    bot.run()