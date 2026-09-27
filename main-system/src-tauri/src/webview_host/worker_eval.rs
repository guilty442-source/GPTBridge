//! worker_eval.rs — script evaluation inside the worker's session webview.
//!
//! ``eval()`` matches Electron executeJavaScript semantics: the completion
//! value of the program is the result (an IIFE would discard it for
//! expression scripts like `1+2`); a returned promise is flattened by
//! ``Promise.resolve``.  Results post back over ``/__result`` so the eval
//! call never holds the webview hostage waiting on a JS return channel.

use std::time::Duration;

use tauri::Manager;

use super::worker::{self, on_main};

pub(super) fn eval_script(script: &str) -> serde_json::Value {
    let request_id = format!("eval-{}", std::process::id());
    let (tx, rx) = std::sync::mpsc::channel::<serde_json::Value>();
    worker::pending_cell()
        .lock()
        .unwrap()
        .insert(request_id.clone(), tx);
    let port = worker::worker_port();
    let token = worker::token_cell().lock().unwrap().clone();
    let wrapper = format!(
        "try{{var __r=eval({script_json});Promise.resolve(__r).then(function(r){{try{{var x=new XMLHttpRequest();x.open('POST','http://127.0.0.1:{port}/__result',true);x.setRequestHeader('Content-Type','text/plain');x.send({token_json}+'\\n'+JSON.stringify({{request_id:{rid_json},result:r===undefined?null:r}}));}}catch(e){{}}}}).catch(function(e){{try{{var x=new XMLHttpRequest();x.open('POST','http://127.0.0.1:{port}/__result',true);x.setRequestHeader('Content-Type','text/plain');x.send({token_json}+'\\n'+JSON.stringify({{request_id:{rid_json},error:String(e)}}));}}catch(e2){{}}}});}}catch(e3){{try{{var y=new XMLHttpRequest();y.open('POST','http://127.0.0.1:{port}/__result',true);y.setRequestHeader('Content-Type','text/plain');y.send({token_json}+'\\n'+JSON.stringify({{request_id:{rid_json},error:String(e3)}}));}}catch(e4){{}}}}",
        script_json = serde_json::to_string(&script).unwrap_or_default(),
        port = port,
        token_json = serde_json::to_string(&token).unwrap_or_default(),
        rid_json = serde_json::to_string(&request_id).unwrap_or_default(),
    );
    let dispatched = on_main(move |app| {
        match app.get_webview_window("session") {
            Some(w) => match w.eval(&wrapper) {
                Ok(()) => serde_json::json!({"ok": true}),
                Err(e) => serde_json::json!({"ok": false, "message": format!("EVAL_FAILED:{e}")}),
            },
            None => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
        }
    });
    if dispatched.get("ok") != Some(&serde_json::Value::Bool(true)) {
        worker::pending_cell().lock().unwrap().remove(&request_id);
        return dispatched;
    }
    match rx.recv_timeout(Duration::from_secs(25)) {
        Ok(payload) => {
            if let Some(error) = payload.get("error") {
                serde_json::json!({"ok": false, "message": error.as_str().unwrap_or("EVAL_ERROR")})
            } else {
                serde_json::json!({"ok": true, "result": payload.get("result").cloned().unwrap_or(serde_json::Value::Null)})
            }
        }
        Err(_) => serde_json::json!({"ok": false, "message": "EVAL_TIMEOUT"}),
    }
}
