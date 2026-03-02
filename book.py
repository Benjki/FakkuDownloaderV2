from dataclasses import dataclass, field


@dataclass
class Book:
    # Core metadata extracted from the info page
    title: str = ''
    author: str = ''
    pages: int = 0
    tags: list[str] = field(default_factory=list)   # genre tags for ComicInfo.xml
    source_url: str = ''                             # normalised FAKKU URL

    # Series membership — all None if this is a one-shot
    series_name: str | None = None
    volume_number: int | None = None
    short_title: str | None = None   # title minus series name prefix

    # Routing flags (set by organizer)
    is_cover: bool = False           # True when pages <= 4
    multi_collection: bool = False   # True when book belongs to >1 FAKKU collection
    missing_volumes: bool = False    # True when preceding series volumes cannot be found

    def is_series(self) -> bool:
        return self.series_name is not None

    def display_name(self) -> str:
        """Human-readable string for logging."""
        if self.is_series():
            return f"{self.series_name} vol.{self.volume_number} - {self.short_title} [{self.author}]"
        return f"{self.title} [{self.author}]"
