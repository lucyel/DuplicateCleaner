"""Read-only sampled-frame viewer used only by Similar files."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QImage
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .files import ensure_current
from .preview import ImageView
from .video_similarity import preview_frame, timestamp


class VideoComparison(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.videos, self.evidence = (), None
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.samples = QComboBox()
        self.samples.setAccessibleName("Video frame comparison time")
        self.samples.currentIndexChanged.connect(self.show_sample)
        layout.addWidget(self.samples)
        row = QHBoxLayout()
        self.views, self.labels = [], []
        for index, title in enumerate(("Reference video", "Candidate video")):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            view = ImageView()
            view.setAccessibleName(title + " sampled frame")
            column.addWidget(view, 1)
            label = QLabel()
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            column.addWidget(label)
            button = QPushButton("Open video")
            button.clicked.connect(lambda checked=False, i=index: self.open_video(i))
            column.addWidget(button)
            row.addLayout(column, 1)
            self.views.append(view)
            self.labels.append(label)
        layout.addLayout(row, 1)

    def set_pair(self, reference, candidate, evidence):
        self.clear()
        self.videos, self.evidence = (reference, candidate), evidence
        self.summary.setText(f"{evidence.matched} / 12 sampled sections match · {evidence.valid} informative sections\n"
                             "Visual comparison only; audio is not compared. Previews are reduced samples.")
        blocked = self.samples.blockSignals(True)
        for a, b, distance in evidence.pairs:
            status = "Low detail" if distance is None else f"Distance {distance} / 63"
            self.samples.addItem(f"~{timestamp(reference.timestamps[a])} ↔ ~{timestamp(candidate.timestamps[b])} · {status}")
        self.samples.blockSignals(blocked)
        self.show_sample(0)

    def show_sample(self, slot):
        if not self.videos or slot < 0:
            return
        a, b, distance = self.evidence.pairs[slot]
        for index, frame in enumerate((a, b)):
            video = self.videos[index]
            try:
                self.views[index].display(preview_frame(video, frame))
                self.views[index].fit()
                self.labels[index].setText(f"{video.record.path.name}\n~{timestamp(video.timestamps[frame])} / {timestamp(video.duration)}")
                self.labels[index].setToolTip(str(video.record.path))
            except OSError as exc:
                self.views[index].display(QImage())
                self.labels[index].setText(str(exc))

    def open_video(self, index):
        if self.videos:
            video = self.videos[index]
            try:
                ensure_current(video.record)
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(video.record.path))):
                    raise OSError("Windows could not open this video")
            except OSError as exc:
                self.labels[index].setText(str(exc))

    def clear(self):
        self.videos, self.evidence = (), None
        self.samples.clear()
        for view in self.views:
            view.display(QImage())
        for label in self.labels:
            label.clear()
