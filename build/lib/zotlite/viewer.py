import subprocess

PDF_VIEWER = 'google-chrome'


def open_pdf(path, page=None):
    """Open a PDF in the viewer without blocking, optionally jumping to a 1-based page."""
    target = f"{path.resolve().as_uri()}#page={page}" if page else str(path)

    subprocess.Popen( [PDF_VIEWER, target]
                    , stdout=subprocess.DEVNULL
                    , stderr=subprocess.DEVNULL
                    )
