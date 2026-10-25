"""Native AppKit popover for the menu-bar player. No HTML or web view."""

import objc
from AppKit import (
    NSApplication, NSBezierPath, NSBox, NSBoxSeparator, NSButton, NSColor, NSFont, NSImage,
    NSImageView, NSMakeRect, NSMakeSize, NSMinYEdge, NSMomentaryChangeButton,
    NSPopover, NSPopoverBehaviorTransient, NSScrollView, NSSlider,
    NSSliderCell, NSTextField, NSTrackingArea, NSTrackingActiveInActiveApp,
    NSTrackingInVisibleRect, NSTrackingMouseEnteredAndExited, NSView,
    NSViewController, NSVisualEffectView, NSVisualEffectMaterialPopover,
    NSVisualEffectBlendingModeBehindWindow, NSVisualEffectStateActive,
    NSLineBreakByTruncatingTail, NSTextAlignmentRight,
    NSFontAttributeName,
)
from Foundation import NSObject, NSString

from library import DEFAULT_SOURCE

WIDTH = 356


def accent():
    return NSColor.colorWithSRGBRed_green_blue_alpha_(0.70, 0.58, 0.84, 1)


def clock_text(seconds):
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


class FlippedView(NSView):
    def isFlipped(self):
        return True


class PlayerSurface(FlippedView):
    def drawRect_(self, rect):
        NSColor.windowBackgroundColor().setFill()
        NSBezierPath.bezierPathWithRect_(rect).fill()


class PassiveLabel(NSTextField):
    def hitTest_(self, point):
        return None


class PassiveImage(NSImageView):
    def hitTest_(self, point):
        return None


class ActionTarget(NSObject):
    def invoke_(self, sender):
        self.callback(sender)


class RowButton(NSButton):
    def drawRect_(self, rect):
        if getattr(self, "selected", False) or (getattr(self, "hovered", False) and self.isEnabled()):
            color = accent().colorWithAlphaComponent_(0.17 if getattr(self, "selected", False) else 0.09)
            color.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 7, 7).fill()
        objc.super(RowButton, self).drawRect_(rect)

    def updateTrackingAreas(self):
        if getattr(self, "tracking", None) is not None:
            self.removeTrackingArea_(self.tracking)
        flags = NSTrackingMouseEnteredAndExited | NSTrackingActiveInActiveApp | NSTrackingInVisibleRect
        self.tracking = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(self.bounds(), flags, self, None)
        self.addTrackingArea_(self.tracking)
        objc.super(RowButton, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        self.hovered = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.hovered = False
        self.setNeedsDisplay_(True)


class PurpleSliderCell(NSSliderCell):
    """Only the drawing is custom; tracking, keys and accessibility stay native."""
    def drawBarInside_flipped_(self, rect, flipped):
        track = NSMakeRect(rect.origin.x, rect.origin.y + (rect.size.height - 4) / 2, rect.size.width, 4)
        NSColor.separatorColor().setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(track, 2, 2).fill()
        track.size.width *= (self.doubleValue() - self.minValue()) / max(1, self.maxValue() - self.minValue())
        accent().setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(track, 2, 2).fill()

    def drawKnob_(self, rect):
        size = 13
        knob = NSMakeRect(rect.origin.x + (rect.size.width - size) / 2,
                          rect.origin.y + (rect.size.height - size) / 2, size, size)
        accent().setFill()
        NSBezierPath.bezierPathWithOvalInRect_(knob).fill()


class SeekSlider(NSSlider):
    def mouseDown_(self, event):
        owner = self.owner
        generation = owner.app.player.generation
        owner.scrubbing = True
        try:
            objc.super(SeekSlider, self).mouseDown_(event)
        finally:
            owner.scrubbing = False
        if generation == owner.app.player.generation:
            owner.app.seek(self.doubleValue())


def label(text, frame, size=13, secondary=False, weight=0):
    view = PassiveLabel.labelWithString_(text)
    view.setFrame_(frame)
    view.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    view.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
    view.setLineBreakMode_(NSLineBreakByTruncatingTail)
    return view


def symbol(name, frame, secondary=False):
    view = PassiveImage.alloc().initWithFrame_(frame)
    view.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None))
    view.setContentTintColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
    return view


class PlayerPopover:
    def __init__(self, app):
        self.app = app
        self.targets = []
        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.popover.setAnimates_(True)
        self.controller = NSViewController.alloc().init()
        self.background = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 430))
        self.background.setMaterial_(NSVisualEffectMaterialPopover)
        self.background.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        self.background.setState_(NSVisualEffectStateActive)
        self.content = PlayerSurface.alloc().initWithFrame_(self.background.bounds())
        self.background.addSubview_(self.content)
        self.controller.setView_(self.background)
        self.popover.setContentViewController_(self.controller)
        self.popover.setContentSize_(NSMakeSize(WIDTH, 430))
        self.signature = None
        self.scrubbing = False
        self.seek_slider = None
        self.status_button = None
        self.render()

    def target(self, callback):
        target = ActionTarget.alloc().init()
        target.callback = callback
        self.targets.append(target)
        return target

    def install(self, status_item):
        status_item.setMenu_(None)
        self.status_button = status_item.button()
        # Keep this target separate from view targets, which are replaced on render.
        self.status_target = ActionTarget.alloc().init()
        self.status_target.callback = self.toggle
        self.status_button.setTarget_(self.status_target)
        self.status_button.setAction_(b"invoke:")
        self.status_button.setToolTip_("Lofi Player")
        self.status_button.setAccessibilityLabel_("Lofi Player")

    def toggle(self, _=None):
        if self.popover.isShown():
            self.close()
            return
        self.show()

    def show(self):
        if self.popover.isShown():
            return
        if self.app._dialog_open:
            return
        self.render()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.popover.showRelativeToRect_ofView_preferredEdge_(self.status_button.bounds(), self.status_button, NSMinYEdge)
        self.content.window().makeKeyWindow()

    def close(self):
        self.popover.performClose_(None)

    def row(self, parent, y, title, icon, callback, badge="", selected=False, enabled=True, width=324):
        button = RowButton.alloc().initWithFrame_(NSMakeRect(0 if parent is not self.content else 16, y, width, 37))
        button.setTitle_("")
        button.setBordered_(False)
        button.setButtonType_(NSMomentaryChangeButton)
        button.setEnabled_(enabled)
        button.selected = selected
        button.setTarget_(self.target(callback))
        button.setAction_(b"invoke:")
        button.setAccessibilityLabel_(title + (", " + badge if badge else ""))
        button.setToolTip_(title)
        button.addSubview_(symbol(icon, NSMakeRect(10, 10, 17, 17), not enabled))
        badge_width = 65 if badge else 0
        if title:
            button.addSubview_(label(title, NSMakeRect(38, 9, width - 49 - badge_width, 20), secondary=not enabled))
        if badge:
            trailing = label(badge, NSMakeRect(width - 77, 11, 66, 17), 11, True)
            trailing.setAlignment_(NSTextAlignmentRight)
            button.addSubview_(trailing)
        parent.addSubview_(button)
        return button

    def divider(self, y):
        line = NSBox.alloc().initWithFrame_(NSMakeRect(22, y, WIDTH - 44, 1))
        line.setBoxType_(NSBoxSeparator)
        self.content.addSubview_(line)

    def update_progress(self):
        app = self.app
        live = app.source.get("is_live")
        if app.status in ("loading", "reconnecting"):
            text = "Connecting…" if app.status == "loading" else "Reconnecting…"
        elif live:
            text = "Live radio" + (" · Channel fallback" if app._fallback else "")
        else:
            duration = app.source.get("duration")
            position = self.seek_slider.doubleValue() if self.scrubbing and self.seek_slider else app.position
            text = f"{clock_text(position)} / {clock_text(duration)}" if duration else "YouTube audio"
            if self.seek_slider and not self.scrubbing:
                self.seek_slider.setDoubleValue_(app.position)
        if app.status == "finished":
            text = "Finished · " + text
        self.time_label.setStringValue_(text)

    def seek_changed(self, sender):
        if self.scrubbing:
            self.update_progress()
        else:
            self.app.seek(sender.doubleValue())

    def render(self):
        app = self.app
        favorites = [DEFAULT_SOURCE] + app.library.items
        signature = (app.status, app.source.get("title"), app.source["url"], app.source.get("is_live"),
                     app._fallback, tuple((s["url"], s["title"]) for s in favorites),
                     app._error_item.title)
        if signature == self.signature:
            self.update_progress()
            if self.slider.intValue() != app.volume:
                self.slider.setIntValue_(app.volume)
            self.volume_label.setStringValue_(f"{app.volume}%")
            return
        self.signature = signature
        for view in list(self.content.subviews()):
            view.removeFromSuperview()
        self.targets = []
        self.seek_slider = None
        self.scrubbing = False
        header = "Connecting" if app.status in ("loading", "reconnecting") else "Now playing" if app.is_playing else "Paused" if app.status == "paused" else "Ready to listen"
        if app.status == "error":
            header = "Playback unavailable"
        self.content.addSubview_(label(header, NSMakeRect(26, 21, 300, 17), 11, True))
        title = app.source.get("title") or "YouTube audio"
        if app.source["url"] == DEFAULT_SOURCE["url"]:
            title = "Lofi Girl"
        title_font = NSFont.systemFontOfSize_weight_(19, 0.23)
        title_height = 24 if NSString.stringWithString_(title).sizeWithAttributes_({NSFontAttributeName: title_font}).width <= 300 else 49
        shift = title_height - 49
        title_view = label(title, NSMakeRect(26, 44, 300, title_height), 19, weight=0.23)
        title_view.setMaximumNumberOfLines_(2)
        title_view.cell().setWraps_(True)
        title_view.setToolTip_(title)
        self.content.addSubview_(title_view)
        self.time_label = label("", NSMakeRect(26, 99 + shift, 302, 18), 12, True)
        self.time_label.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0))
        self.content.addSubview_(self.time_label)
        self.update_progress()
        busy = app.status in ("loading", "reconnecting")
        live = app.source.get("is_live")
        duration = app.source.get("duration")
        if live is False and duration:
            self.seek_slider = SeekSlider.alloc().initWithFrame_(NSMakeRect(26, 123 + shift, 304, 22))
            self.seek_slider.owner = self
            self.seek_slider.setCell_(PurpleSliderCell.alloc().init())
            self.seek_slider.setMinValue_(0)
            self.seek_slider.setMaxValue_(duration)
            self.seek_slider.setDoubleValue_(app.position)
            self.seek_slider.setContinuous_(True)
            self.seek_slider.setEnabled_(app.status in ("playing", "paused"))
            self.seek_slider.setTarget_(self.target(self.seek_changed))
            self.seek_slider.setAction_(b"invoke:")
            self.seek_slider.setAccessibilityLabel_("Playback position in seconds")
            self.seek_slider.setToolTip_("Drag to seek · release to play from this point")
            self.content.addSubview_(self.seek_slider)
            shift += 30
        action = "Stop" if busy or (live and app.is_playing) else "Pause" if app.is_playing else "Resume" if app.status == "paused" else "Play"
        icon = {"Stop":"stop", "Pause":"pause", "Resume":"play", "Play":"play"}[action]
        self.row(self.content, 127 + shift, action, icon, app.toggle, width=190 if live is False else 240)
        if live is False and not busy:
            for x, symbol_name, delta, description in (
                (219, "gobackward.15", -15, "Back 15 seconds"),
                (261, "goforward.15", 15, "Forward 15 seconds"),
            ):
                skip = self.row(self.content, 127 + shift, "", symbol_name,
                                lambda _, delta=delta: app.seek(app.position + delta), width=37,
                                enabled=app.status in ("playing", "paused"))
                skip.setFrameOrigin_((x, 127 + shift))
                skip.setToolTip_(description)
                skip.setAccessibilityLabel_(description)
            restart = self.row(self.content, 127 + shift, "", "arrow.counterclockwise", app._restart, width=37)
            restart.setFrameOrigin_((303, 127 + shift))
            restart.setToolTip_("Restart from Beginning")
            restart.setAccessibilityLabel_("Restart from Beginning")
        self.content.addSubview_(symbol("speaker.wave.2", NSMakeRect(26, 178 + shift, 17, 17)))
        self.slider = NSSlider.alloc().initWithFrame_(NSMakeRect(57, 173 + shift, 223, 26))
        self.slider.setCell_(PurpleSliderCell.alloc().init())
        self.slider.setMinValue_(0)
        self.slider.setMaxValue_(100)
        self.slider.setIntValue_(app.volume)
        self.slider.setContinuous_(True)
        self.slider.setTarget_(self.target(lambda sender: app._on_volume_changed(int(sender.intValue()))))
        self.slider.setAction_(b"invoke:")
        self.slider.setAccessibilityLabel_("Volume")
        self.content.addSubview_(self.slider)
        self.volume_label = label(f"{app.volume}%", NSMakeRect(290, 179 + shift, 41, 17), 11, True)
        self.volume_label.setAlignment_(NSTextAlignmentRight)
        self.content.addSubview_(self.volume_label)
        self.divider(216 + shift)
        self.row(self.content, 227 + shift, "Play YouTube URL…", "link", app._paste_url)
        saved = app.library.contains(app.source["url"])
        self.row(self.content, 266 + shift, "Saved to Favorites" if saved else "Save Current to Favorites…",
                 "checkmark" if saved else "star", app._save_current, enabled=not saved)
        y = 315 + shift
        if app.status == "error":
            error = label(app._error_item.title, NSMakeRect(26, y, 302, 34), 11, True)
            error.setMaximumNumberOfLines_(2)
            error.setToolTip_(app._error_item._menuitem.toolTip())
            self.content.addSubview_(error)
            y += 38
            self.row(self.content, y, "Retry", "arrow.clockwise", app._on_retry_clicked)
            y += 39
            if not app._solve_item._menuitem.isHidden():
                self.row(self.content, y, "Solve YouTube challenge…", "person.crop.circle.badge.exclamationmark", app._on_solve_clicked)
                y += 39
        self.divider(y)
        self.content.addSubview_(label("Favorites", NSMakeRect(26, y + 17, 290, 17), 11, True, 0.2))
        y += 44
        # Keep long libraries reachable without allowing the popover off-screen.
        list_height = min(len(favorites), 4 if app.status != "error" else 2) * 38
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(16, y, 324, list_height))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(len(favorites) * 38 > list_height)
        document = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 324, len(favorites) * 38))
        scroll.setDocumentView_(document)
        for index, source in enumerate(favorites):
            selected = source["url"] == app.source["url"]
            default = index == 0
            badge = "Playing" if selected and app.status == "playing" else "Paused" if selected and app.status == "paused" else "LIVE" if default or (selected and live) else ""
            icon = "waveform" if selected and app.is_playing else "dot.radiowaves.left.and.right" if default else "music.note"
            name = "Lofi Girl" if default else source["title"]
            self.row(document, index * 38, name, icon, lambda _, source=source: app._select_source(source), badge, selected)
        self.content.addSubview_(scroll)
        y += list_height + 13
        self.divider(y)
        self.row(self.content, y + 12, "Manage Favorites…", "list.bullet", app._manage_favorites, width=265)
        quit_button = self.row(self.content, y + 12, "", "power", app.quit_app, width=37)
        quit_button.setFrameOrigin_((303, y + 12))
        quit_button.setToolTip_("Quit Lofi Player")
        quit_button.setAccessibilityLabel_("Quit Lofi Player")
        height = y + 64
        self.content.setFrame_(NSMakeRect(0, 0, WIDTH, height))
        self.background.setFrameSize_(NSMakeSize(WIDTH, height))
        self.popover.setContentSize_(NSMakeSize(WIDTH, height))
