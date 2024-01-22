"""
This file defines the Interpreter class.
It's the main file. `from interpreter import interpreter` will import an instance of this class.
"""

import json
import os
from datetime import datetime

from ..terminal_interface.terminal_interface import terminal_interface
from ..terminal_interface.utils.local_storage_path import get_storage_path
from .computer.computer import Computer
from .default_system_message import default_system_message
from .extend_system_message import extend_system_message
from .llm.llm import Llm
from .respond import respond
from .utils.telemetry import send_telemetry
from .utils.truncate_output import truncate_output


class OpenInterpreter:
    """
    This class (one instance is called an `interpreter`) is the "grand central station" of this project.

    Its responsibilities are to:

    1. Given some user input, prompt the language model.
    2. Parse the language models responses, converting them into LMC Messages.
    3. Send code to the computer.
    4. Parse the computer's response (which will already be LMC Messages).
    5. Send the computer's response back to the language model.
    ...

    The above process should repeat—going back and forth between the language model and the computer— until:

    6. Decide when the process is finished based on the language model's response.
    """

    def __init__(
        self,
        messages=None,
        offline=False,
        auto_run=False,
        verbose=False,
        max_output=2800,
        safe_mode="off",
        shrink_images=False,
        force_task_completion=False,
        anonymous_telemetry=os.getenv("ANONYMIZED_TELEMETRY", "True") == "True",
        in_terminal_interface=False,
        conversation_history=True,
        conversation_filename=None,
        conversation_history_path=get_storage_path("conversations"),
        os=False,
        speak_messages=False,
        llm=None,
        system_message=default_system_message,
        custom_instructions="",
        computer=None,
    ):
        # State
        self.messages = [] if messages is None else messages

        # Settings
        self.offline = offline
        self.auto_run = auto_run
        self.verbose = verbose
        self.max_output = max_output
        self.safe_mode = safe_mode
        self.shrink_images = shrink_images
        self.force_task_completion = force_task_completion
        self.anonymous_telemetry = anonymous_telemetry
        self.in_terminal_interface = in_terminal_interface

        # Conversation history
        self.conversation_history = conversation_history
        self.conversation_filename = conversation_filename
        self.conversation_history_path = conversation_history_path

        # OS control mode related attributes
        self.os = os
        self.speak_messages = speak_messages

        # LLM
        self.llm = Llm(self) if llm is None else llm

        # These are LLM related
        self.system_message = system_message
        self.custom_instructions = custom_instructions

        # Computer
        self.computer = Computer() if computer is None else computer

    def chat(self, message=None, display=True, stream=False):
        if self.vision:
            self.model = "gpt-4-vision-preview"
            self.system_message += "\nThe user will show you an image of the code you write. You can view images directly. Be sure to actually write a markdown code block for almost every user request! Almost EVERY message should include a markdown code block. Do not end your message prematurely!\n\nFor HTML: This will be run STATELESSLY. You may NEVER write '<!-- previous code here... --!>' or `<!-- header will go here -->` or anything like that. It is CRITICAL TO NEVER WRITE PLACEHOLDERS. Placeholders will BREAK it. You must write the FULL HTML CODE EVERY TIME. Therefore you cannot write HTML piecemeal—write all the HTML, CSS, and possibly Javascript **in one step, in one code block**. The user will help you review it visually.\nIf the user submits a filepath, you will also see the image. The filepath and user image will both be in the user's message."
            self.function_calling_llm = False
            self.context_window = 110000
            self.max_tokens = 4096
            self.force_task_completion = True

        try:
            if self.anonymous_telemetry and not self.offline:
                message_type = type(
                    message
                ).__name__  # Only send message type, no content
                send_telemetry(
                    "started_chat",
                    properties={
                        "in_terminal_interface": self.in_terminal_interface,
                        "message_type": message_type,
                        "os_mode": self.os,
                    },
                )

            if stream:
                return self._streaming_chat(message=message, display=display)

            initial_message_count = len(self.messages)

            # If stream=False, *pull* from the stream.
            for _ in self._streaming_chat(message=message, display=display):
                pass

            # Return new messages
            return self.messages[initial_message_count:]

        except Exception as e:
            if self.anonymous_telemetry and not self.offline:
                message_type = type(message).__name__
                send_telemetry(
                    "errored",
                    properties={
                        "error": str(e),
                        "in_terminal_interface": self.in_terminal_interface,
                        "message_type": message_type,
                        "os_mode": self.os,
                    },
                )

            raise

    def _streaming_chat(self, message=None, display=True):
        # Sometimes a little more code -> a much better experience!
        # Display mode actually runs interpreter.chat(display=False, stream=True) from within the terminal_interface.
        # wraps the vanilla .chat(display=False) generator in a display.
        # Quite different from the plain generator stuff. So redirect to that
        if display:
            yield from terminal_interface(self, message)
            return

        # One-off message
        if message or message == "":
            if message == "":
                message = "No entry from user - please suggest something to enter."

            ## We support multiple formats for the incoming message:
            # Dict (these are passed directly in)
            if isinstance(message, dict):
                if "role" not in message:
                    message["role"] = "user"
                self.messages.append(message)
            # String (we construct a user message dict)
            elif isinstance(message, str):
                self.messages.append(
                    {"role": "user", "type": "message", "content": message}
                )
            # List (this is like the OpenAI API)
            elif isinstance(message, list):
                self.messages = message

            # DISABLED because I think we should just not transmit images to non-multimodal models?
            # REENABLE this when multimodal becomes more common:

            # Make sure we're using a model that can handle this
            # if not self.llm.supports_vision:
            #     for message in self.messages:
            #         if message["type"] == "image":
            #             raise Exception(
            #                 "Use a multimodal model and set `interpreter.llm.supports_vision` to True to handle image messages."
            #             )

            # This is where it all happens!
            yield from self._respond_and_store()

            # Save conversation if we've turned conversation_history on
            if self.conversation_history:
                # If it's the first message, set the conversation name
                if not self.conversation_filename:
                    first_few_words = "_".join(
                        self.messages[0]["content"][:25].split(" ")[:-1]
                    )
                    for char in '<>:"/\\|?*!':  # Invalid characters for filenames
                        first_few_words = first_few_words.replace(char, "")

                    date = datetime.now().strftime("%B_%d_%Y_%H-%M-%S")
                    self.conversation_filename = (
                        "__".join([first_few_words, date]) + ".json"
                    )

                # Check if the directory exists, if not, create it
                if not os.path.exists(self.conversation_history_path):
                    os.makedirs(self.conversation_history_path)
                # Write or overwrite the file
                with open(
                    os.path.join(
                        self.conversation_history_path, self.conversation_filename
                    ),
                    "w",
                ) as f:
                    json.dump(self.messages, f)
            return

        raise Exception(
            "`interpreter.chat()` requires a display. Set `display=True` or pass a message into `interpreter.chat(message)`."
        )

    def _respond_and_store(self):
        """
        Pulls from the respond stream, adding delimiters. Some things, like active_line, console, confirmation... these act specially.
        Also assembles new messages and adds them to `self.messages`.
        """

        # Utility function
        def is_active_line_chunk(chunk):
            return "format" in chunk and chunk["format"] == "active_line"

        last_flag_base = None

        for chunk in respond(self):
            if chunk["content"] == "":
                continue

            # Handle the special "confirmation" chunk, which neither triggers a flag or creates a message
            if chunk["type"] == "confirmation":
                # Emit a end flag for the last message type, and reset last_flag_base
                if last_flag_base:
                    yield {**last_flag_base, "end": True}
                    last_flag_base = None
                yield chunk
                # We want to append this now, so even if content is never filled, we know that the execution didn't produce output.
                # ... rethink this though.
                self.messages.append(
                    {
                        "role": "computer",
                        "type": "console",
                        "format": "output",
                        "content": "",
                    }
                )
                continue

            # Check if the chunk's role, type, and format (if present) match the last_flag_base
            if (
                last_flag_base
                and "role" in chunk
                and "type" in chunk
                and last_flag_base["role"] == chunk["role"]
                and last_flag_base["type"] == chunk["type"]
                and (
                    "format" not in last_flag_base
                    or (
                        "format" in chunk
                        and chunk["format"] == last_flag_base["format"]
                    )
                )
            ):
                # If they match, append the chunk's content to the current message's content
                # (Except active_line, which shouldn't be stored)
                if not is_active_line_chunk(chunk):
                    self.messages[-1]["content"] += chunk["content"]
            else:
                # If they don't match, yield a end message for the last message type and a start message for the new one
                if last_flag_base:
                    yield {**last_flag_base, "end": True}

                last_flag_base = {"role": chunk["role"], "type": chunk["type"]}

                # Don't add format to type: "console" flags, to accomodate active_line AND output formats
                if "format" in chunk and chunk["type"] != "console":
                    last_flag_base["format"] = chunk["format"]

                yield {**last_flag_base, "start": True}

                # Add the chunk as a new message
                if not is_active_line_chunk(chunk):
                    self.messages.append(chunk)

            # Yield the chunk itself
            yield chunk

            # Truncate output if it's console output
            if chunk["type"] == "console" and chunk["format"] == "output":
                self.messages[-1]["content"] = truncate_output(
                    self.messages[-1]["content"], self.max_output
                )

        # Yield a final end flag
        if last_flag_base:
            yield {**last_flag_base, "end": True}

    def reset(self):
        self.computer.terminate()  # Terminates all languages

        # Reset the function below, in case the user set it
        self.extend_system_message = lambda: extend_system_message(self)

        self.__init__()

    # These functions are worth exposing to developers
    # I wish we could just dynamically expose all of our functions to devs...
    def extend_system_message(self):
        return extend_system_message(self)
