# Jenkins Metadata Parser

Parses/acts on various bits of metadata available in the upstream [Jenkins](jenkins.io) project,
including update centers and package repositories.

Once installed, its CLI functionality should be exposed via the 'jm' executable.

This project is narrowly focused on specific requirements related to supporting custom-made
distributions of jenkins on a specific support timeline, and is likely not generally useful.

That said the [LICENSE](LICENSE) and [COPYRIGHT](COPYRIGHT) should allow for any changes you
might want provided those changes themselves comply with the terms of the GPLv3 license.

While this library is certainly able to be distributed via PyPI, its complete lack of generic
usefulness means that it probably should not be distributed in that way.
