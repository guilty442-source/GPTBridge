extern crate webview2_com_sys;
pub use webview2_com_sys::Microsoft;

#[macro_use]
extern crate webview2_com_macros;

mod callback;
mod options;
mod pwstr;

use std::{fmt, sync::mpsc};

use windows::{
    core::HRESULT,
    Win32::UI::WindowsAndMessaging::{self, MSG},
};

pub use callback::*;
pub use options::*;
pub use pwstr::*;

#[derive(Debug)]
pub enum Error {
    WindowsError(windows::core::Error),
    CallbackError(String),
    TaskCanceled,
    SendError,
}

impl std::error::Error for Error {}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{self:?}")
    }
}

impl From<windows::core::Error> for Error {
    fn from(err: windows::core::Error) -> Self {
        Self::WindowsError(err)
    }
}

impl From<HRESULT> for Error {
    fn from(err: HRESULT) -> Self {
        Self::WindowsError(windows::core::Error::from(err))
    }
}

pub type Result<T> = std::result::Result<T, Error>;

/// The WebView2 threading model runs everything on the UI thread, including callbacks which it triggers
/// with `PostMessage`, and we're using this here because it's waiting for some async operations in WebView2
/// to finish before starting the main message loop. As long as there are no pending results in `rx`, it
/// will pump Window messages and check for a result after each message is dispatched.
///
/// GPTBridge patch (wry #1665 / #583): the original implementation used a raw
/// `GetMessage`/`DispatchMessage` loop.  A raw Win32 pump does NOT dispatch COM
/// apartment calls, so `CreateCoreWebView2Controller`'s STA completion callback
/// is never delivered once the event loop is already running — deterministic
/// deadlock when creating a second WebView2 controller.  We instead use
/// `CoWaitForMultipleHandles` with `COWAIT_DISPATCH_CALLS | COWAIT_DISPATCH_WINDOW_MESSAGES`,
/// the COM-sanctioned wait that dispatches both window messages and incoming
/// COM calls, plus a `PeekMessage` pass to preserve `WM_QUIT` semantics and a
/// bounded timeout so the result channel is re-checked periodically.
pub fn wait_with_pump<T>(rx: mpsc::Receiver<T>) -> Result<T> {
    use windows::Win32::System::Com::{
        CoWaitForMultipleHandles, COWAIT_DISPATCH_CALLS, COWAIT_DISPATCH_WINDOW_MESSAGES,
    };
    use windows::Win32::UI::WindowsAndMessaging::{PM_REMOVE, WM_QUIT};

    let mut msg = MSG::default();

    loop {
        if let Ok(result) = rx.try_recv() {
            return Ok(result);
        }

        unsafe {
            // Drain any already-queued window messages (keeps the UI thread
            // responsive) and preserve the original WM_QUIT cancellation
            // contract.
            while WindowsAndMessaging::PeekMessageA(&mut msg, None, 0, 0, PM_REMOVE).into() {
                if msg.message == WM_QUIT {
                    return Err(Error::TaskCanceled);
                }
                let _ = WindowsAndMessaging::TranslateMessage(&msg);
                WindowsAndMessaging::DispatchMessageA(&msg);
            }

            let mut index = 0u32;
            // COM-sanctioned STA wait: dispatches incoming COM calls (which
            // WebView2 completion handlers require) AND window messages.
            // Bounded timeout so the result channel is re-checked.
            let _hr = CoWaitForMultipleHandles(
                COWAIT_DISPATCH_CALLS | COWAIT_DISPATCH_WINDOW_MESSAGES,
                50,
                0,
                std::ptr::null(),
                &mut index,
            );
        }
    }
}
