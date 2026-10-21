# Maintainer: shuka0158 <shukakidze.davit2010@gmail.com>
pkgname=ytm-tui
pkgver=1.5.2
pkgrel=1
pkgdesc="A terminal music player for your YouTube Music account"
arch=('x86_64' 'aarch64')
url="https://github.com/shuka0158/ytm-tui"
license=('GPL3')
depends=(
    'python>=3.11'
    'mpv'
    'ffmpeg'
    'python-pillow'
    'python-requests'
    'python-secretstorage'
    'python-textual'
    'python-textual-image'
    'python-yt-dlp'
    'python-ytmusicapi'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-setuptools'
    'python-wheel'
)
source=("https://github.com/shuka0158/ytm-tui/archive/v${pkgver}.tar.gz")
sha256sums=('a6261e57fbe762e9306d8879db4cb261ee24cc415eace6a6d3741e6dcbb3b8bc')

build() {
    cd "$pkgname-$pkgver"
    python -m build --wheel --no-isolation
}

package() {
    cd "$pkgname-$pkgver"
    python -m installer --destdir="$pkgdir" dist/*.whl
}
