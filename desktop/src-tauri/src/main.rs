//! Kelp desktop shell: main window, frameless pet overlay, and the summonable
//! command panel.
//!
//! No chat logic lives here — the webview talks to the one configured Kelp
//! server URL, and this scaffold renders canned replies until the backend
//! wiring lands. Unsigned dev builds only: no updater keys, no signing step.
//! No telemetry: the shell collects nothing and phones nowhere.

#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use tauri::Manager;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

/// Summon / dismiss the panel. Both the avatar click and the global shortcut
/// route here, so the two entry points cannot drift apart.
const OVERLAY_SHORTCUT: &str = "CommandOrControl+Shift+Space";

fn toggle_overlay(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("overlay") else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
    } else {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_stronghold::Builder::new(|salt| salt.as_bytes().to_vec()).build())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        toggle_overlay(app);
                    }
                })
                .build(),
        )
        .setup(|app| {
            app.global_shortcut().register(OVERLAY_SHORTCUT)?;

            // Desk pet rides on all workspaces (macOS). Elsewhere the
            // always-on-top frameless window is the whole story.
            #[cfg(target_os = "macos")]
            if let Some(pet) = app.get_webview_window("pet") {
                let _ = pet.set_visible_on_all_workspaces(true);
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run kelp desktop");
}
