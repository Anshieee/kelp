//! Kelp desktop shell: main window, frameless pet, and the summonable panel.
//!
//! The panel is a separate window so the orb stays clickable whether or not it
//! is open. It is positioned beside the pet every time it is summoned, sized to
//! the compact bar, and flipped to the other side when the pet sits near a
//! screen edge. Clicking the pet or the global shortcut runs the same toggle.
//!
//! No chat logic lives here. Unsigned dev builds only: no updater keys, no
//! signing step. No telemetry.

#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use tauri::{LogicalSize, Manager, PhysicalPosition};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

const OVERLAY_SHORTCUT: &str = "CommandOrControl+Shift+Space";
/// Panel width in logical pixels. The webview keeps this width in both states.
const PANEL_WIDTH: f64 = 400.0;
/// Compact bar height in logical pixels. Expanded height is driven from JS.
const PANEL_COMPACT_HEIGHT: f64 = 76.0;

/// Park the panel next to the orb: to its right by default, to its left when
/// there is no room, always inside the pet's monitor work area.
fn place_beside_pet(app: &tauri::AppHandle, window: &tauri::WebviewWindow) {
    let Some(pet) = app.get_webview_window("pet") else {
        return;
    };
    let (Ok(pet_pos), Ok(pet_size), Ok(scale)) = (
        pet.outer_position(),
        pet.outer_size(),
        window.scale_factor(),
    ) else {
        return;
    };

    let panel_w = (PANEL_WIDTH * scale).round() as i32;
    let panel_h = (PANEL_COMPACT_HEIGHT * scale).round() as i32;
    let gap = (12.0 * scale).round() as i32;

    let bounds = pet
        .current_monitor()
        .ok()
        .flatten()
        .map(|monitor| {
            let origin = monitor.position();
            let size = monitor.size();
            (
                origin.x,
                origin.y,
                origin.x + size.width as i32,
                origin.y + size.height as i32,
            )
        })
        .unwrap_or((0, 0, i32::MAX, i32::MAX));
    let (min_x, min_y, max_x, max_y) = bounds;

    let mut x = pet_pos.x + pet_size.width as i32 + gap;
    if x + panel_w > max_x - 8 {
        x = pet_pos.x - panel_w - gap;
    }
    if x < min_x + 8 {
        x = min_x + 8;
    }

    let mut y = pet_pos.y;
    if y + panel_h > max_y - 8 {
        y = max_y - panel_h - 8;
    }
    if y < min_y + 8 {
        y = min_y + 8;
    }

    let _ = window.set_position(PhysicalPosition::new(x, y));
}

/// Summon the panel: reset to the compact bar, park it beside the orb, focus it.
fn show_panel(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("overlay") else {
        return;
    };
    let _ = window.set_size(LogicalSize::new(PANEL_WIDTH, PANEL_COMPACT_HEIGHT));
    place_beside_pet(app, &window);
    let _ = window.show();
    let _ = window.set_focus();
}

/// The one toggle: pet click and global shortcut both land here.
fn toggle_panel(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("overlay") else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
    } else {
        show_panel(app);
    }
}

/// Exposed to the pet window so a tap on the orb summons or dismisses the panel.
#[tauri::command]
fn toggle_overlay(app: tauri::AppHandle) {
    toggle_panel(&app);
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_stronghold::Builder::new(|salt| salt.as_bytes().to_vec()).build())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        toggle_panel(app);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![toggle_overlay])
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
