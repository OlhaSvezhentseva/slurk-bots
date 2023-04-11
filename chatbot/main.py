# -*- coding: utf-8 -*-
# University of Potsdam
"""Commandline interface."""

import argparse
import logging
import os

from lib.chatbot import Chatbot


if __name__ == "__main__":
    # set up logging configuration
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s:%(message)s")

    # create commandline parser
    parser = argparse.ArgumentParser(description="Run Bot.")

    # collect environment variables as defaults
    if "SLURK_TOKEN" in os.environ:
        token = {"default": os.environ["SLURK_TOKEN"]}
    else:
        token = {"required": True}
    if "SLURK_USER" in os.environ:
        user = {"default": os.environ["SLURK_USER"]}
    else:
        user = {"required": True}
    if "SLURK_WAITING_ROOM" in os.environ:
        waiting_room = {"default": os.environ["SLURK_WAITING_ROOM"]}
    else:
        waiting_room = {"required": True}
    host = {"default": os.environ.get("SLURK_HOST", "http://localhost")}
    port = {"default": os.environ.get("SLURK_PORT")}
    task_id = {"default": os.environ.get("TASK_ID")}

    # register commandline arguments
    parser.add_argument(
        "-t",
        "--token",
        help="token for logging in as bot",
        **token
    )
    parser.add_argument(
        "-u",
        "--user",
        type=int,
        help="user id for the bot",
        **user
    )
    parser.add_argument(
        "-c",
        "--host",
        help="full URL (protocol, hostname) of chat server",
        **host
    )
    parser.add_argument(
        "--waiting_room",
        type=int,
        help="room where users enter at first",
        **waiting_room
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        help="port of slurk chat server", **port
    )
    parser.add_argument("--task_id", type=int, help="task to join", **task_id)

    args = parser.parse_args()

    # create bot instance
    chatbot = Chatbot(args.token, args.user, args.task_id, args.host, args.port)
    chatbot.waiting_room = args.waiting_room

    # connect to slurk server
    chatbot.run()
