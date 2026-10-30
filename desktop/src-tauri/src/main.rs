//! Kelp desktop shell: main window + frameless pet overlay.
//! No chat logic lives here — the webview talks to the one configured
//! Kelp server URL. Unsigned dev builds only: no updater keys, no signing
//! step. No telemetry: the shell collects nothing and phones nowhere.

#![cfg_attr(not(debug_assertions), windows_subsystem_windows)]

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_stronghold::Builder::new(|salt| salt.as_bytes().to_vec()).build())
        .setup(|_app| {
            // Desk pet rides on all workspaces (macOS). Elsewhere the
            // always-on-top frameless window is the whole story.
            #[cfg(target_os = "macos")]
            if let Some(pet) = _app.get_webview_window("pet") {
                let _ = pet.set_visible_on_all_workspaces(true);
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run kelp desktop");
}
