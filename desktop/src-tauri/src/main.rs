//! Kelp desktop shell: main window, frameless pet, and the summonable panel.
//!
//! The pet is the avatar: it lives at the bottom right of the primary monitor
//! and carries no window chrome. The panel opens to its **left**, bottom edges
//! aligned, so growing it upward never pushes it off screen. Clicking the pet
//! or the global shortcut runs the same toggle.
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
/// Pet window side in logical pixels: a small floating orb, not a panel.
const PET_SIZE: f64 = 96.0;

/// Park the panel to the left of the orb with the bottom edges aligned, inside
/// the pet's monitor. Falls back to the right side only when the left is out of
/// room (a pet parked against the left edge).
fn place_beside_pet(app: &tauri::AppHandle, window: &tauri::WebviewWindow, width: f64, height: f64) {
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

    let panel_w = (width * scale).round() as i32;
    let panel_h = (height * scale).round() as i32;
    let gap = (12.0 * scale).round() as i32;

    let (min_x, min_y, max_x, max_y) = pet
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

    // Default: to the left of the orb.
    let mut x = pet_pos.x - panel_w - gap;
    if x < min_x + 8 {
        x = pet_pos.x + pet_size.width as i32 + gap;
    }
    if x + panel_w > max_x - 8 {
        x = max_x - panel_w - 8;
    }
    if x < min_x + 8 {
        x = min_x + 8;
    }

    // Bottom edges aligned: the panel grows upward as it expands.
    let mut y = pet_pos.y + pet_size.height as i32 - panel_h;
    if y < min_y + 8 {
        y = min_y + 8;
    }
    if y + panel_h > max_y - 8 {
        y = max_y - panel_h - 8;
    }

    let _ = window.set_position(PhysicalPosition::new(x, y));
}

/// Summon the panel: reset to the compact bar, park it beside the orb, focus it.
fn show_panel(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("overlay") else {
        return;
    };
    let _ = window.set_size(LogicalSize::new(PANEL_WIDTH, PANEL_COMPACT_HEIGHT));
    place_beside_pet(app, &window, PANEL_WIDTH, PANEL_COMPACT_HEIGHT);
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

/// Resize and immediately re-park, so expanding keeps the bottom edge stable
/// instead of growing down past the screen.
#[tauri::command]
fn resize_overlay(app: tauri::AppHandle, width: f64, height: f64) {
    let Some(window) = app.get_webview_window("overlay") else {
        return;
    };
    let _ = window.set_size(LogicalSize::new(width, height));
    place_beside_pet(&app, &window, width, height);
}

/// Hide the panel when focus leaves it — unless the orb or the main window took
/// focus. Those two are part of the app, and the orb runs its own toggle; hiding
/// here first would make that toggle re-show the panel instead of closing it.
fn watch_overlay_blur(app: &tauri::AppHandle) {
    let Some(overlay) = app.get_webview_window("overlay") else {
        return;
    };
    let handle = app.clone();
    overlay.on_window_event(move |event| {
        if !matches!(event, tauri::WindowEvent::Focused(false)) {
            return;
        }
        let app = handle.clone();
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(140));
            let ours = ["pet", "main"].iter().any(|label| {
                app.get_webview_window(label)
                    .and_then(|w| w.is_focused().ok())
                    .unwrap_or(false)
            });
            if ours {
                return;
            }
            if let Some(window) = app.get_webview_window("overlay") {
                let _ = window.hide();
            }
        });
    });
}

/// Park the orb at the bottom right of the primary monitor.
fn place_pet(app: &tauri::AppHandle) {
    let Some(pet) = app.get_webview_window("pet") else {
        return;
    };
    let Ok(Some(monitor)) = pet.primary_monitor() else {
        return;
    };
    // Pin the size too: some platforms pad a frameless window past the
    // configured width, leaving an invisible strip beside the orb.
    let _ = pet.set_size(LogicalSize::new(PET_SIZE, PET_SIZE));
    let scale = monitor.scale_factor();
    let origin = monitor.position();
    let size = monitor.size();
    let pet_px = (PET_SIZE * scale).round() as i32;
    let margin_x = (24.0 * scale).round() as i32;
    let margin_y = (96.0 * scale).round() as i32;
    let x = origin.x + size.width as i32 - pet_px - margin_x;
    let y = origin.y + size.height as i32 - pet_px - margin_y;
    let _ = pet.set_position(PhysicalPosition::new(x, y));
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
        .invoke_handler(tauri::generate_handler![toggle_overlay, resize_overlay])
        .setup(|app| {
            app.global_shortcut().register(OVERLAY_SHORTCUT)?;
            place_pet(app.handle());
            watch_overlay_blur(app.handle());

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
