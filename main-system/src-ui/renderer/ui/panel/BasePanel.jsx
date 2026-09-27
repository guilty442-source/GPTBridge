import { useCallback, useEffect, useRef, useState } from "react";
import { PanelDrawer } from "./PanelDrawer.jsx";
import { mainSystemLocale } from "@/locales/main-system";
import "./BasePanel.css";
const t = mainSystemLocale.toolbox;
const DEFAULT_TIMEOUT_MS = 3e4;
export function BasePanel({ open, onClose, sendCommand, waitForIpcEvent, backendSocket, children, title, eyebrow, icon, headerActions, side = "right" }) {
	const [state, setState] = useState({
		loading: false,
		error: "",
		connected: backendSocket.status === "Connected"
	});
	const mountedRef = useRef(true);
	// Update connection status
	useEffect(() => {
		setState((prev) => ({
			...prev,
			connected: backendSocket.status === "Connected"
		}));
	}, [backendSocket.status]);
	// Cleanup on unmount
	useEffect(() => {
		mountedRef.current = true;
		return () => {
			mountedRef.current = false;
		};
	}, []);
	const setLoading = useCallback((loading) => {
		setState((prev) => ({
			...prev,
			loading
		}));
	}, []);
	const setError = useCallback((error) => {
		setState((prev) => ({
			...prev,
			error
		}));
	}, []);
	const clearError = useCallback(() => {
		setState((prev) => ({
			...prev,
			error: ""
		}));
	}, []);
	const send = useCallback((command, payload) => {
		return sendCommand(command, payload);
	}, [sendCommand]);
	const waitForEvent = useCallback(async (eventName, timeoutMs = 3e4, predicate) => {
		return waitForIpcEvent(eventName, timeoutMs, predicate);
	}, [waitForIpcEvent]);
	const generateRequestId = useCallback((prefix = "panel") => {
		return `${prefix}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
	}, []);
	// Extract locale strings for common actions
	const locale = {
		launch: mainSystemLocale.toolbox.start,
		launching: mainSystemLocale.toolbox.starting,
		stop: mainSystemLocale.toolbox.stop,
		closing: mainSystemLocale.toolbox.stopping,
		status: mainSystemLocale.toolbox.status,
		statusStopped: mainSystemLocale.toolbox.statusStopped,
		statusRunning: mainSystemLocale.toolbox.statusRunning,
		statusError: mainSystemLocale.toolbox.statusError,
		errorFetch: mainSystemLocale.toolbox.errorFetch,
		errorTimeout: mainSystemLocale.toolbox.errorTimeout,
		disconnected: mainSystemLocale.toolbox.disconnected,
		refresh: mainSystemLocale.toolbox.refresh,
		loading: mainSystemLocale.toolbox.loading
	};
	return <PanelDrawer open={open} onClose={onClose} title={title} eyebrow={eyebrow} icon={icon} side={side} headerActions={headerActions}>
      <div className="base-panel">
        {state.error && <div className="base-panel__error" role="alert">
            {state.error}
            <button type="button" className="base-panel__error-dismiss" onClick={clearError} aria-label={mainSystemLocale.common.close}>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M4 4L10 10M10 4L4 10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </button>
          </div>}

        {!state.connected && <div className="base-panel__disconnected">
            {locale.disconnected}
          </div>}

        {children({
		state,
		locale,
		send,
		waitForEvent,
		generateRequestId,
		setLoading,
		setError,
		clearError,
		connected: state.connected,
		loading: state.loading
	})}
      </div>
    </PanelDrawer>;
}
