#!/usr/bin/env python3

# Copyright (c) 2008 Aldo Cortesi
# Copyright (c) 2011 Mounier Florian
# Copyright (c) 2012 dmpayton
# Copyright (c) 2014 Sean Vig
# Copyright (c) 2014 roger
# Copyright (c) 2014 Pedro Algarvio
# Copyright (c) 2014-2015 Tycho Andersen
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

import sys
import textwrap

from setuptools import setup
from setuptools.command.install import install


def check_dependencies():
    dependencies = {
        'cairocffi': 'cairocffi>=1.2.0',
        'xcffib': 'xcffib>=0.5.0',
        'xcb-util-cursor': 'xcb-util-cursor>=0.1.3',
        'libmpdclient': 'python-mpd2>=2.0.0',
        'xdotool': 'xdotool>=0.0.20160718'
    }
    
    missing_deps = []
    
    for package, requirement in dependencies.items():
        try:
            __import__(package)
        except ImportError:
            missing_deps.append(requirement)
    
    if missing_deps:
        print(
            textwrap.dedent(f"""
                Some required dependencies are missing. To install, run:
                pip install {' '.join(missing_deps)}
            """)
        )
        sys.exit(1)


class CheckDependencies(install):
    def run(self):
        check_dependencies()
        install.run(self)


setup(
    name='qtile',
    version='0.18.0',
    packages=['libqtile'],
    include_package_data=True,
    cmdclass={'install': CheckDependencies},
    install_requires=[
        'cairocffi>=1.2.0',
        'xcffib>=0.5.0',
        'xcb-util-cursor>=0.1.3',
        'python-mpd2>=2.0.0',
        'xdotool>=0.0.20160718'
    ],
)
