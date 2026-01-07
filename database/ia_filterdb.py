import logging
from struct import pack
import re
import base64

from pyrogram.file_id import FileId
from pymongo.errors import DuplicateKeyError
from umongo import Instance, Document, fields
from motor.motor_asyncio import AsyncIOMotorClient
from marshmallow.exceptions import ValidationError

from info import DATABASE_URI, DATABASE_NAME, COLLECTION_NAME, USE_CAPTION_FILTER

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------
# Normalization (NEW)
# ---------------------------

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_QUALITY_RE = re.compile(r"\b(360p|480p|720p|1080p|1440p|2160p|4k|8k)\b", re.I)
_AUDIO_RE = re.compile(r"\b(5\s*\.?\s*1|7\s*\.?\s*1|2\s*\.?\s*0)\b", re.I)

_JUNK_RE = re.compile(
    r"\b(web\s*dl|webrip|bluray|brrip|hdrip|hdtv|dvdrip|remux|proper|repack|extended|unrated)\b"
    r"|\b(x264|x265|h\.?264|h\.?265|hevc|aac|dts|ddp?\d\.\d)\b",
    re.I,
)

_LANG_RE = re.compile(r"\b(hindi|english|punjabi|arabic|french|spanish|turkish|dual|multi)\b", re.I)


def normalize_title(text: str) -> str:
    """Normalize a raw filename/title into a clean search key.
    Example: 'La.La.Land.2016.1080p.WEB-DL.5.1' => 'la la land'
    """
    if not text:
        return ""
    s = str(text).lower()
    s = s.replace("&", " and ")
    # Replace separators with spaces
    s = re.sub(r"[._\-+]+", " ", s)
    # Remove year/quality/audio/junk/languages
    s = _YEAR_RE.sub(" ", s)
    s = _QUALITY_RE.sub(" ", s)
    s = _AUDIO_RE.sub(" ", s)
    s = _JUNK_RE.sub(" ", s)
    s = _LANG_RE.sub(" ", s)
    # Keep only letters/digits/spaces
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    # Collapse spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


client = AsyncIOMotorClient(DATABASE_URI)
db = client[DATABASE_NAME]
instance = Instance.from_db(db)


@instance.register
class Media(Document):
    file_id = fields.StrField(attribute='_id')
    file_ref = fields.StrField(allow_none=True)

    # Original filename for display
    file_name = fields.StrField(required=True)

    # NEW: normalized title used for search
    title_norm = fields.StrField(required=True)

    file_size = fields.IntField(required=True)
    file_type = fields.StrField(allow_none=True)
    mime_type = fields.StrField(allow_none=True)
    caption = fields.StrField(allow_none=True)

    class Meta:
        # Keep old text index on file_name + add normal index on title_norm
        indexes = ('title_norm', '$file_name')
        collection_name = COLLECTION_NAME


async def save_file(media):
    """Save file in database"""

    # TODO: Find better way to get same file_id for same media to avoid duplicates
    file_id, file_ref = unpack_new_file_id(media.file_id)

    raw_name = str(getattr(media, "file_name", "") or "")
    title_norm = normalize_title(raw_name)

    # If normalization is empty, fallback to something safe (prevents ValidationError)
    if not title_norm:
        # try caption if available (rare cases like '2 mp4')
        cap = None
        try:
            cap = media.caption.html if media.caption else None
        except Exception:
            cap = None
        title_norm = normalize_title(cap or raw_name) or raw_name.lower().strip() or "unknown"

    try:
        file = Media(
            file_id=file_id,
            file_ref=file_ref,
            file_name=raw_name,
            title_norm=title_norm,
            file_size=media.file_size,
            file_type=media.file_type,
            mime_type=media.mime_type,
            caption=media.caption.html if media.caption else None,
        )
    except ValidationError:
        logger.exception('Error occurred while saving file in database')
        return False, 2
    else:
        try:
            await file.commit()
        except DuplicateKeyError:
            logger.warning(
                f'{getattr(media, "file_name", "NO_FILE")} is already saved in database'
            )
            return False, 0
        else:
            logger.info(f'{getattr(media, "file_name", "NO_FILE")} is saved to database')
            return True, 1


async def get_search_results(query, file_type=None, max_results=7, offset=0, filter=False):
    """For given query return (results, next_offset, total_results)

    IMPORTANT: This now searches in `title_norm` (normalized).
    """

    query = (query or "").strip()
    qn = normalize_title(query)

    if not qn:
        raw_pattern = r"."
    else:
        parts = [re.escape(p) for p in qn.split() if p]
        # build a tolerant pattern: word1.*word2.*word3
        raw_pattern = r".*".join(parts) if parts else r"."

    try:
        regex = re.compile(raw_pattern, flags=re.IGNORECASE)
    except Exception:
        return [], '', 0

    if USE_CAPTION_FILTER:
        filter_doc = {'$or': [{'title_norm': regex}, {'caption': regex}]}
    else:
        filter_doc = {'title_norm': regex}

    if file_type:
        filter_doc['file_type'] = file_type

    total_results = await Media.count_documents(filter_doc)
    next_offset = offset + max_results

    if next_offset > total_results:
        next_offset = ''

    cursor = Media.find(filter_doc)
    # Sort by recent
    cursor.sort('$natural', -1)
    # Slice files according to offset and max results
    cursor.skip(offset).limit(max_results)
    # Get list of files
    files = await cursor.to_list(length=max_results)

    return files, next_offset, total_results


async def get_file_details(query):
    filter_doc = {'file_id': query}
    cursor = Media.find(filter_doc)
    filedetails = await cursor.to_list(length=1)
    return filedetails


def encode_file_id(s: bytes) -> str:
    r = b""
    n = 0

    for i in s + bytes([22]) + bytes([4]):
        if i == 0:
            n += 1
        else:
            if n:
                r += b"\x00" + bytes([n])
                n = 0

            r += bytes([i])

    return base64.urlsafe_b64encode(r).decode().rstrip("=")


def encode_file_ref(file_ref: bytes) -> str:
    return base64.urlsafe_b64encode(file_ref).decode().rstrip("=")


def unpack_new_file_id(new_file_id):
    """Return file_id, file_ref"""
    decoded = FileId.decode(new_file_id)
    file_id = encode_file_id(
        pack(
            "<iiqq",
            int(decoded.file_type),
            decoded.dc_id,
            decoded.media_id,
            decoded.access_hash
        )
    )
    file_ref = encode_file_ref(decoded.file_reference)
    return file_id, file_ref
