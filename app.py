"""A chat-only interface: discuss dimensions, approve them, then build native CAD."""
import threading
from datetime import datetime

import streamlit as st

from cad_builders import build_command
from groq_client import ROOT, chat, is_confirmation, proposal_text, validate_command

st.set_page_config(page_title="MechGen", page_icon="⚙️", layout="centered")
st.set_option("client.toolbarMode", "minimal")
# Hide Streamlit's header chrome, including Deploy and the three-dot menu.
st.markdown("""
<style>
[data-testid="stHeader"], [data-testid="stToolbar"], #MainMenu, footer {
    display: none !important;
}
</style>
""", unsafe_allow_html=True)
st.title("MechGen")


@st.cache_resource
def build_lock():
    return threading.Lock()


if "cad_messages" not in st.session_state:
    st.session_state.cad_messages = [{
        "role": "assistant",
        "content": "Hi! I am MechGen. I can help you create a flange or mounting plate. "
                   "Which part would you like? Share your dimensions, or I can help you choose them.",
    }]
    st.session_state.pending_part = None

for message in st.session_state.cad_messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Describe your part…", max_chars=8000):
    st.session_state.cad_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        bubble = st.empty()
        pending = st.session_state.pending_part
        st.session_state.pending_part = None
        if pending is not None and is_confirmation(prompt):
            lock = build_lock()
            if not lock.acquire(blocking=False):
                reply = "Another build is in progress. Please wait, then reply 'build' again."
                st.session_state.pending_part = pending
            else:
                try:
                    validate_command(pending)
                    path = ROOT / "models" / (pending["function"].removeprefix("build_") + "_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".SLDPRT")
                    bubble.markdown("Building your part in SOLIDWORKS…")
                    result = build_command(
                        pending, output_path=path, step_delay=2.5,
                        on_stage=lambda stage: bubble.markdown("Building your part… " + stage),
                    )
                    reply = f"Your part has been created and saved. Solid-body and dimension checks passed.\n\n`{result['path']}`\n\nLet me know if you would like to create another flange or plate."
                except Exception as exc:
                    reply = f"The build did not complete: {exc}\n\nMake sure SOLIDWORKS is open and ready. If a partial part was created, inspect it first; I will not automatically retry the build."
                finally:
                    lock.release()
        else:
            bubble.markdown("Thinking…")
            try:
                response = chat(st.session_state.cad_messages[-30:])
                reply = response["message"]
                if response["action"] == "confirm":
                    try:
                        command = validate_command(response["command"])
                    except Exception as exc:
                        reply = f"The part is not ready to build: {exc}\n\nWhat corrected value should I use?"
                    else:
                        st.session_state.pending_part = command
                        reply += "\n\n" + proposal_text(command)
            except Exception as exc:
                reply = f"{exc}\n\nNo part was built. Please try sending your message again."
        bubble.markdown(reply)
        st.session_state.cad_messages.append({"role": "assistant", "content": reply})
