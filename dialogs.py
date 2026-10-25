"""Small AppKit dialogs. No web UI or additional runtime."""

import objc
import sys
from AppKit import (
    NSAlert, NSApplication, NSCommandKeyMask, NSShiftKeyMask, NSKeyDown,
    NSModalPanelWindowLevel, NSMakeRect, NSView, NSTextField,
    NSPopUpButton,
)
from Foundation import NSObject


class EditingField(NSTextField):
    """Accessory apps have no Edit menu to dispatch the usual shortcuts."""
    def performKeyEquivalent_(self, event):
        if event.type() == NSKeyDown and event.modifierFlags() & NSCommandKeyMask:
            key = event.charactersIgnoringModifiers().lower()
            action = {"x": "cut:", "c": "copy:", "v": "paste:", "a": "selectAll:", "z": "undo:"}.get(key)
            if key == "z" and event.modifierFlags() & NSShiftKeyMask:
                action = "redo:"
            if action:
                return NSApplication.sharedApplication().sendAction_to_from_(action, None, self)
        return objc.super(EditingField, self).performKeyEquivalent_(event)


def run_alert(alert):
    app = NSApplication.sharedApplication()
    # Accessory apps can own key windows. Changing activation policy while a
    # status menu is dismissing races focus and can leave an inactive modal
    # window behind other apps, making the status menu appear frozen.
    alert.layout()
    window = alert.window()
    window.setLevel_(NSModalPanelWindowLevel)
    window.center()
    app.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)
    window.orderFrontRegardless()
    print(f"dialog opened: {alert.messageText()}", file=sys.stderr, flush=True)
    try:
        return alert.runModal() - 1000
    finally:
        window.orderOut_(None)
        print(f"dialog closed: {alert.messageText()}", file=sys.stderr, flush=True)


def message(title, detail):
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(detail)
    alert.addButtonWithTitle_("OK")
    run_alert(alert)


def text_dialog(title, description, initial="", button="Save", save_option=False):
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(description)
    alert.addButtonWithTitle_(button)
    alert.addButtonWithTitle_("Cancel")
    field = EditingField.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 26))
    field.setStringValue_(initial)
    alert.setAccessoryView_(field)
    alert.window().setInitialFirstResponder_(field)
    if save_option:
        alert.setShowsSuppressionButton_(True)
        alert.suppressionButton().setTitle_("Save to favorites")
        alert.suppressionButton().setState_(0)
    result = run_alert(alert)
    if result != 0:
        return None
    field.validateEditing()
    return field.stringValue().strip(), bool(save_option and alert.suppressionButton().state())


class FavoritePicker(NSObject):
    def changed_(self, sender):
        index = sender.indexOfSelectedItem()
        self.field.setStringValue_(self.items[index]["title"])


def manage_dialog(items, selected=0):
    """Return an action and selection; caller persists then reopens after edits."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Manage Favorites")
    alert.setInformativeText_("Lofi Girl stays pinned first. Choose another favorite to rename, reorder, or remove.")
    for title in ("Done", "Rename", "Move Up", "Move Down", "Remove"):
        alert.addButtonWithTitle_(title)
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 94))
    picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 57, 360, 30), False)
    for item in items:
        # Native pop-up buttons deduplicate equal titles; number them here only.
        picker.addItemWithTitle_(f"{picker.numberOfItems() + 1}. {item['title']}")
    picker.selectItemAtIndex_(min(selected, len(items) - 1))
    field = EditingField.alloc().initWithFrame_(NSMakeRect(0, 10, 360, 26))
    field.setStringValue_(items[picker.indexOfSelectedItem()]["title"])
    target = FavoritePicker.alloc().init()
    target.items, target.field = items, field
    picker.setTarget_(target)
    picker.setAction_(b"changed:")
    view.addSubview_(picker)
    view.addSubview_(field)
    alert.setAccessoryView_(view)
    result = run_alert(alert)
    field.validateEditing()
    return ("done", "rename", "up", "down", "remove")[result], picker.indexOfSelectedItem(), field.stringValue().strip()
