//! Kelp desktop shell: main window + frameless pet overlay.
//! No chat logic lives here — the webview talks to the Kelp server URL.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_stronghold::Builder::new(|salt| salt.to_vec()).build())
        .run(tauri::generate_context!())
        .expect("failed to run kelp desktop");
}
