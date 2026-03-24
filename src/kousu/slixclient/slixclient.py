import sys
import os
import logging
import asyncio
import readline
import threading # yes asyncio, but we use threading to be able to call input()
import argparse
from getpass import getpass
import copy
import xml.etree.ElementTree as ElementTree

import xdg.BaseDirectory

import slixmpp
from slixmpp import JID

# BUG: Ctrl-D doesn't go to the EOFError case, instead it falls out through the TaskGroup

log = logging.getLogger(__name__)


class XMPPLogger(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.msg.startswith("RECV: ") or record.msg.startswith("SEND: "):
            # pretty-print the XML
            xml, = record.args

            if hasattr(xml, 'pretty_print'):
                xml = copy.deepcopy(xml.xml)
            elif isinstance(xml, str):
                try:
                    xml = ElementTree.fromstring(xml)
                except ElementTree.ParseError:
                    return

            ElementTree.indent(xml)
            xml = slixmpp.xmlstream.tostring(xml)
            record.args = ("\n\n" + xml + "\n",)

        self.flush()

logging.getLogger("slixmpp.xmlstream.xmlstream").addHandler(XMPPLogger())

# -----

class Prompt:
    """
        A prompt-aware stdout stream

        Inspired by https://pypi.org/project/prompt-toolkit/ but only as complex as needed for this project.
    """
    _input = input
    def __init__(self, wrapped=sys.stdout):
        self._wrapped = wrapped
        self.prompt = None
        self._buf = ""
        self._lock = threading.Lock()

    def input(self, prompt=None):
        try:
            self.prompt = prompt
            return self._input(prompt)
        finally:
            self.prompt = None

    def write(self, text: str):
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n",1)
                self._wrapped.write("\r\033[K")   # return to col 0, erase line
                self._wrapped.write(line+"\n")
                # Redraw prompt + in-progress input
                self._wrapped.write(f"{self.prompt if self.prompt else ""}{readline.get_line_buffer()}")
                self._wrapped.flush()

    def flush(self):
        self._wrapped.flush()

    def __getattr__(self, name):
        # Delegate everything else
        return getattr(self._wrapped, name)

prompt = Prompt()
input = prompt.input
sys.stdout = prompt

# ----------------------------------------------------

class Client(slixmpp.ClientXMPP):
    def __init__(self, jid, password=None):
        super().__init__(jid, password)

        self.register_plugin("xep_0045") # group chats
        self.register_plugin("xep_0359") # stanza-id
        self.register_plugin("xep_0444") # reactions
        self.register_plugin("xep_0461") # replies

        self.add_event_handler("session_start", self.on_start)
        self.add_event_handler("message", self.on_message)
        self.add_event_handler("reactions", self.on_reactions)

        self.archive = {} # message archive

    async def on_start(self, event):
        self.send_presence()
        await self.get_roster()

    async def on_message(self, msg):
        if msg["type"] not in ("chat", "normal", "groupchat"):
            # not a message (it's some metadata that got shoved in <message> over the years)
            return
        if msg["type"] == "groupchat" and msg["delay"]["from"] == msg["from"].bare:
            # scrollback; not a live message; ignore
            return

        # https://xmpp.org/extensions/xep-0359.html
        # > they can be used together with Message Archive Management (XEP-0313) [1] to uniquely identify a message within an archive. They are also useful in the context of Multi-User Chat (XEP-0045) [2] conferences, as they allow to identify a message reflected by a MUC service back to the originating entity.
        # https://xmpp.org/extensions/xep-0461.html#business-id
        # > For messages of type 'groupchat', the stanza's 'id' attribute MUST NOT be used for replies. Instead, in group chat situations, the ID assigned to the stanza by the group chat itself must be used. This is discovered in a <stanza-id> element with a 'by' attribute that matches the bare JID of the group chat, as defined in Unique and Stable Stanza IDs (XEP-0359) [4].
        # > This implies that group chat messages without a Unique and Stable Stanza IDs (XEP-0359) [4] stanza-id cannot be replied to.
        # > For other message types the sender should use the 'id' from a Unique and Stable Stanza IDs (XEP-0359) [4] <origin-id> if present, or the value of the 'id' attribute on the <message> otherwise.
        # https://xmpp.org/extensions/xep-0444.html#sending-reactions
        # > referred to by including its ID or in MUCs its stanza-id as defined in Unique and Stable Stanza IDs (XEP-0359) [6]
        # breakpoint()
        msgid = msg["stanza_id"]["id"] if msg["type"] == "groupchat" else (msg["origin_id"]["id"] or msg["id"])
        self.archive[msgid] = msg

        if msg['body']:
            if msg['reply']['id']:
                reply_id = msg['reply']['id']
            else:
                reply_id = None
            print(f"[{msgid}] {msg['from']}: {'[re: ' + reply_id + '] ' if reply_id else ''}{msg['body']}")

    async def on_reactions(self, msg):
        if msg["type"] == "groupchat" and msg["delay"]["from"] == msg["from"].bare:
            # scrollback; not a live message; ignore
            return

        msgid = msg["stanza_id"]["id"] if msg["type"] == "groupchat" else (msg["origin_id"]["id"] or msg["id"])

        reply_id = msg['reactions']['id']
        reactions = msg["reactions"]["values"]
        print(f"[{msgid}] {msg['from']}: {'[re: ' + reply_id + '] ' if reply_id else ''}{reactions}")


# -------------------------------------

async def menu(xmpp):
    histfile = os.path.join(xdg.BaseDirectory.save_data_path("kousu-xmpp", xmpp.boundjid.bare), "history")
    try:
        readline.read_history_file(histfile)
    except FileNotFoundError:
        pass
    except Exception:
        import traceback
        traceback.print_exc()
        breakpoint()

    try:
        while True:
            cmd = await asyncio.get_event_loop().run_in_executor(None, input, "> ")
            try:
                parts = cmd.split(maxsplit=1)
                cmd = parts[0].strip() if len(parts)>0 else None
                rest = parts[1] if len(parts)>1 else None

                print(cmd, rest)

                if cmd == "help":
                    print("do i look like i'm made of money?")
                elif cmd in ["exit", "quit"]:
                    xmpp.disconnect()
                    return
                elif cmd == "msg":
                    dst, msg = rest.split(maxsplit=1)
                    if dst in xmpp["xep_0045"].get_joined_rooms():
                        type = 'groupchat'
                    else:
                        type = None
                    xmpp.send_message(mto=JID(dst), mbody=msg, mtype=type)
                elif cmd == "reply":
                    reply_to, body = rest.split(maxsplit=1)
                    orig = xmpp.archive[reply_to]
                    msg = orig.reply(body) # XXX .reply() != XEP 461 replies, it just sets the from and to correctly
                    msg["reply"]["to"] = orig["from"]
                    msg["reply"]["id"] = reply_to
                    msg.send()
                elif cmd == "react":
                    reply_id, emoji = rest.split(maxsplit=1)
                    emoji = [e.strip() for e in emoji.split(", ")]
                    m = xmpp.archive[reply_id].reply()
                    xmpp["xep_0444"].set_reactions(m, reply_id, emoji)
                    m.send()
                elif cmd == "join":
                    room = rest
                    await xmpp["xep_0045"].join_muc_wait(room, xmpp.boundjid.user)
                elif cmd == "log":
                    if rest == "on":
                        logging.getLogger().setLevel(logging.DEBUG)
                    elif rest == "off":
                        logging.getLogger().setLevel(logging.WARN)
                elif cmd == "break":
                    print("use 'c' to return to the main menu")
                    breakpoint()
            except asyncio.CancelledError:
                print("Cancelled")
                pass
            except EOFError:
                print("^D") # XXX doesn't work
                break
            except Exception:
                import traceback
                traceback.print_exc()
                pass
    finally:
        os.makedirs(os.path.dirname(histfile), exist_ok=True)
        readline.write_history_file(histfile)


parser = argparse.ArgumentParser(
    description="""CLI XMPP client for debugging.

    Provide XMPP credentials in
    - XMPP_JID
    - XMPP_PASSWORD

    or on the CLI (it will prompt)
    """,
)
parser.add_argument(
    "username",
    nargs="?",
    default=os.environ.get("XMPP_JID"),
    help="XMPP JID (default: $XMPP_JID)",
)

parser.add_argument(
    "-v", "--verbose", action="count", default=0, help="Enable verbose logging"
)


async def amain():
    args = parser.parse_args()

    if args.verbose > 0:
        logging.getLogger().setLevel(logging.INFO)
    if args.verbose > 1:
        logging.getLogger().setLevel(logging.DEBUG)

    print(f"Connecting to {args.username}.")

    password = os.environ.get("XMPP_PASSWORD", "")
    if not password:
        password = getpass(echo_char="*")

    xmpp = Client(args.username, password)

    try:
        xmpp.connect()
        login = asyncio.create_task(xmpp.wait_until("session_start", timeout=5))

        async def failed_auth(event):
           print(f"Unable to login as '{xmpp.boundjid.bare}'. Check your password.", flush=True)
           login.cancel()

        xmpp.add_event_handler('failed_all_auth', failed_auth)

        await login
        print(f"Logged in as '{xmpp.boundjid.bare}'")
    except TimeoutError:
        print("timed out connecting")
        return
    except (asyncio.CancelledError,EOFError):
        return
        await xmpp.disconnect()

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(menu(xmpp))

            # block until the bot shuts down
            await xmpp.disconnected
    except (asyncio.CancelledError,EOFError):
        await xmpp.disconnect()

def main():
  logging.basicConfig(
      level=logging.WARN,
      format="%(asctime)s %(levelname)-6s %(name)+25s:%(lineno)d: %(message)s",
  )

  asyncio.run(amain())
