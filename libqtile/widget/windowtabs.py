# Copyright (c) 2012-2013 Craig Barnes
# Copyright (c) 2012 roger
# Copyright (c) 2012, 2014 Tycho Andersen
# Copyright (c) 2014 Sean Vig
# Copyright (c) 2014 Adi Sieker
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from libqtile import bar, hook, pangocffi
from libqtile.log_utils import logger
from libqtile.widget import base


class WindowTabs(base._TextBox):
    """
    Displays the name of each window in the current group.
    Contrary to TaskList this is not an interactive widget.
    The window that currently has focus is highlighted.
    """

    defaults = [
        ("separator", " | ", "Task separator text."),
        ("selected", ("<b>", "</b>"), "Selected task indicator"),
        (
            "parse_text",
            None,
            "Function to parse and modify window names. "
            "e.g. function in config that removes excess "
            "strings from window name: "
            "def my_func(text)"
            '    for string in [" - Chromium", " - Firefox"]:'
            '        text = text.replace(string, "")'
            "   return text"
            "then set option parse_text=my_func",
        ),
    ]

    def __init__(self, **config):
        width = config.pop("width", bar.STRETCH)
        base._TextBox.__init__(self, width=width, **config)
        self.add_defaults(WindowTabs.defaults)
        if not isinstance(self.selected, (tuple, list)):
            self.selected = (self.selected, self.selected)

    def _configure(self, qtile, bar):
        base._TextBox._configure(self, qtile, bar)
        hook.subscribe.client_name_updated(self.update)
        hook.subscribe.focus_change(self.update)
        hook.subscribe.float_change(self.update)
        self.add_callbacks({"Button1": self.bar.screen.group.next_window})

    def update(self, *args):
        names = []
        for w in self.bar.screen.group.windows:
            state = ""
            if w.maximized:
                state = "[] "
            elif w.minimized:
                state = "_ "
            elif w.floating:
                state = "V "
            task = "%s%s" % (state, w.name if w and w.name else " ")
            task = pangocffi.markup_escape_text(task)
            if w is self.bar.screen.group.current_window:
                task = task.join(self.selected)
            names.append(task)
        self.text = self.separator.join(names)
        if callable(self.parse_text):
            try:
                self.text = self.parse_text(self.text)
            except:  # noqa: E722
                logger.exception("parse_text function failed:")
        self.bar.draw()
