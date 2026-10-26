"""WebUI login gate and password settings (see app/services/webui_auth.py)."""

import time

import streamlit as st
from loguru import logger

from app.services import webui_auth

AUTH_SESSION_KEY = "mpt_authenticated"


def _logout():
    st.session_state[AUTH_SESSION_KEY] = False


def require_login(tr) -> bool:
    """Render the login form when needed; return True once access is granted."""
    if not webui_auth.is_enabled() or st.session_state.get(AUTH_SESSION_KEY):
        return True
    _, center, _ = st.columns([1, 1.2, 1])
    with center:
        st.markdown("### MoneyPrinterTurbo")
        remaining = webui_auth.lockout_remaining()
        if remaining:
            st.error(tr("access.locked").format(seconds=remaining))
        with st.form("webui_login_form"):
            username = st.text_input(tr("access.username"), value=webui_auth.configured_username())
            password = st.text_input(tr("access.password"), type="password")
            submitted = st.form_submit_button(tr("access.login"), type="primary", use_container_width=True)
        if submitted:
            if webui_auth.check_credentials(username, password):
                st.session_state[AUTH_SESSION_KEY] = True
                logger.info("WebUI login succeeded")
                st.rerun()
            # A short pause makes password guessing slower without hurting users.
            time.sleep(1)
            logger.warning("WebUI login failed")
            st.error(tr("access.invalid"))
        st.caption(tr("access.session_hint"))
    return False


def render_logout_button(tr) -> None:
    if webui_auth.is_enabled() and st.session_state.get(AUTH_SESSION_KEY):
        st.button(
            tr("access.logout"),
            key="webui_logout_button",
            icon=":material/logout:",
            width="content",
            on_click=_logout,
        )


def render_access_settings(tr, set_runtime_config, delete_runtime_config, save_runtime_config) -> None:
    """Settings tab to set, change or remove the WebUI password."""
    st.write(tr("access.settings_intro"))
    if webui_auth.is_managed_by_environment():
        st.info(tr("access.managed_by_env"))
        return
    if webui_auth.is_enabled():
        st.success(tr("access.enabled").format(username=webui_auth.configured_username()))
    else:
        st.warning(tr("access.disabled"))
    with st.form("webui_access_form"):
        username = st.text_input(tr("access.username"), value=webui_auth.configured_username())
        password = st.text_input(tr("access.new_password"), type="password")
        confirmation = st.text_input(tr("access.confirm_password"), type="password")
        submitted = st.form_submit_button(tr("access.save_password"), type="primary")
    if submitted:
        error = webui_auth.validate_new_password(password, confirmation)
        if error:
            st.error(tr(f"access.error.{error}").format(minimum=webui_auth.MIN_PASSWORD_LENGTH))
        else:
            set_runtime_config("ui", "webui_username", username.strip() or webui_auth.DEFAULT_USERNAME)
            set_runtime_config("ui", "webui_password_hash", webui_auth.hash_password(password))
            save_runtime_config()
            # Keep the current admin signed in after enabling protection.
            st.session_state[AUTH_SESSION_KEY] = True
            st.success(tr("access.saved"))
    if webui_auth.is_enabled() and st.button(tr("access.remove_password"), key="webui_remove_password"):
        delete_runtime_config("ui", "webui_password_hash")
        save_runtime_config()
        st.success(tr("access.removed"))
