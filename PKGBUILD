# Maintainer: shuka0158 <shukakidze.davit2010@gmail.com>
pkgname=ytm-tui
pkgver=1.5.0
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
sha256sums=('SKIP')

build() {
    cd "$pkgname-$pkgver"
    python -m build --wheel --no-isolation
}

package() {
    cd "$pkgname-$pkgver"
    python -m installer --destdir="$pkgdir" dist/*.whl
}
