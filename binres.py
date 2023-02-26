#!/usr/bin/python3
# encoding: utf-8
# SPDX-FileCopyrightText: 2023 FC Stegerman <flx@obfusk.net>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
parse/dump android binary XML (AXML) or resources (ARSC)

NB: work in progress; output format may change.

>>> dump("test/data/AndroidManifest.xml")
XML
  STRING POOL [flags=0, #strings=16, #styles=0]
  XML RESOURCE MAP [#resources=6]
  XML NS START [lineno=1, prefix='android', uri='http://schemas.android.com/apk/res/android']
    XML ELEM START [lineno=1, name='manifest']
      ATTR: http://schemas.android.com/apk/res/android:versionCode=1
      ATTR: http://schemas.android.com/apk/res/android:versionName='1'
      ATTR: http://schemas.android.com/apk/res/android:compileSdkVersion=29
      ATTR: http://schemas.android.com/apk/res/android:compileSdkVersionCodename='10.0.0'
      ATTR: package='com.example'
      ATTR: platformBuildVersionCode=29
      ATTR: platformBuildVersionName='10.0.0'
      XML ELEM START [lineno=2, name='uses-sdk']
        ATTR: http://schemas.android.com/apk/res/android:minSdkVersion=21
        ATTR: http://schemas.android.com/apk/res/android:targetSdkVersion=29
      XML ELEM END [lineno=2, name='uses-sdk']
    XML ELEM END [lineno=1, name='manifest']
  XML NS END [lineno=1, prefix='android', uri='http://schemas.android.com/apk/res/android']

>>> dump_apk("test/data/golden-aligned-in.apk", "resources.arsc")
entry='resources.arsc'
RESOURCE TABLE
  STRING POOL [flags=256, #strings=3, #styles=0]
  PACKAGE [id=0x7f, package_name='android.appsecurity.cts.tinyapp']
    STRING POOL [flags=256, #strings=2, #styles=0]
    STRING POOL [flags=256, #strings=1, #styles=0]
    TYPE SPEC [id=0x1, #resources=0]
    TYPE SPEC [id=0x2, #resources=1]
    TYPE [id=0x2, configuration=BinResCfg()]
      ENTRY [id=0x7f020000, key='app_name']
        VALUE: 'Tiny App for CTS'
    TYPE [id=0x2, configuration=BinResCfg()]
      ENTRY [id=0x7f020000, key='app_name']
        VALUE: '[Ţîñý Åþþ ƒöŕ ÇŢŠ one two three]'
    TYPE [id=0x2, configuration=BinResCfg()]
      ENTRY [id=0x7f020000, key='app_name']
        VALUE: '\u200f\u202eTiny\u202c\u200f \u200f\u202eApp\u202c\u200f \u200f\u202efor\u202c\u200f \u200f\u202eCTS\u202c\u200f'

>>> dump("test/data/AndroidManifest.xml", xml=True)
<manifest xmlns:android="http://schemas.android.com/apk/res/android" android:versionCode="1" android:versionName="1" android:compileSdkVersion="29" android:compileSdkVersionCodename="10.0.0" package="com.example" platformBuildVersionCode="29" platformBuildVersionName="10.0.0">
  <uses-sdk android:minSdkVersion="21" android:targetSdkVersion="29" />
</manifest>

>>> dump("test/data/network_security_config.xml", xml=True)
<network-security-config>
  <base-config cleartextTrafficPermitted="true" />
  <domain-config cleartextTrafficPermitted="false">
    <domain includeSubdomains="true">amazonaws.com</domain>
  </domain-config>
  <domain-config cleartextTrafficPermitted="false">
    <domain includeSubdomains="true">f-droid.org</domain>
  </domain-config>
  <domain-config cleartextTrafficPermitted="false">
    <domain includeSubdomains="true">github.com</domain>
  </domain-config>
  <domain-config cleartextTrafficPermitted="false">
    <domain includeSubdomains="true">githubusercontent.com</domain>
  </domain-config>
  <domain-config cleartextTrafficPermitted="false">
    <domain includeSubdomains="true">github.io</domain>
  </domain-config>
  <domain-config cleartextTrafficPermitted="false">
    <domain includeSubdomains="true">gitlab.com</domain>
  </domain-config>
  <domain-config cleartextTrafficPermitted="false">
    <domain includeSubdomains="true">gitlab.io</domain>
  </domain-config>
</network-security-config>

"""

from __future__ import annotations

import dataclasses
import io
import json as _json
import logging
import os
import re
import struct
import sys
import weakref
import zipfile
import zlib

from collections import namedtuple
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from functools import cached_property
from typing import (cast, Any, BinaryIO, Callable, ClassVar, Dict, Iterator,
                    List, Optional, TextIO, Tuple, Union, TYPE_CHECKING)

# https://android.googlesource.com/platform/tools/base
#   apkparser/binary-resources/src/main/java/com/google/devrel/gmscore/tools/apk/arsc/*.java
# https://android.googlesource.com/platform/frameworks/base
#   libs/androidfw/include/androidfw/ResourceTypes.h
#   tools/aapt2/ResourceValues.cpp

ARSC_MAGIC = b"\x02\x00\x0c\x00"
AXML_MAGIC = b"\x03\x00\x08\x00"

MANIFEST = "AndroidManifest.xml"
ARSC_FILE = "resources.arsc"
AXML_FILES = (MANIFEST, "res/*.xml")

SCHEMA_ANDROID = "http://schemas.android.com/apk/res/android"
UTF8, UTF16 = ("utf8", "utf_16_le")

ZipData = namedtuple("ZipData", ("cd_offset", "eocd_offset", "cd_and_eocd"))


class Error(Exception):
    """Base class for errors."""


class ParseError(Error):
    """Parse failure."""


class ParentError(Error):
    """Missing/deallocated parent."""


class ChildError(Error):
    """Missing child."""


class ZipError(Error):
    """Something wrong with ZIP file."""


@dataclass(frozen=True)
class Chunk:
    """Base class for chunks."""
    header_size: int
    chunk_size: int
    parent: Optional[ChunkRef] = field(repr=False, compare=False)
    level: int = field(compare=False)
    offset: int = field(compare=False)

    TYPE_ID: ClassVar[Optional[int]] = None

    @classmethod
    def _parse(_cls, header: bytes, payload: bytes, **kwargs: Any) -> Dict[str, Any]:
        header_size = len(header) + 8
        chunk_size = len(payload) + header_size
        return dict(header_size=header_size, chunk_size=chunk_size, **kwargs)

    @property
    def type_id(self) -> int:
        if self.__class__.TYPE_ID is not None:
            return self.__class__.TYPE_ID
        raise NotImplementedError("No .TYPE_ID or custom .type_id")


@dataclass(frozen=True)
class ParentChunk(Chunk):
    """Base class for chunks with children."""
    children: Tuple[Tuple[int, Chunk], ...] = field(repr=False, compare=False)

    @classmethod
    def _parse(_cls, header: bytes, payload: bytes, **kwargs: Any) -> Dict[str, Any]:
        return Chunk._parse(header=header, payload=payload, children=None, **kwargs)

    def _parse_children(self, payload: bytes) -> None:
        c = tuple(read_chunks(payload, weakref.ref(self), self.level + 1, self.header_size))
        object.__setattr__(self, "children", c)


# FIXME: unused
@dataclass(frozen=True)
class NullChunk(Chunk):
    """Null chunk."""
    TYPE_ID: ClassVar[int] = 0x0000


@dataclass(frozen=True)
class StringPoolChunk(Chunk):
    """String pool."""
    flags: int
    strings: Tuple[str, ...]
    styles: Tuple[Style, ...]

    TYPE_ID: ClassVar[int] = 0x0001

    FLAG_SORTED: ClassVar[int] = 0x1
    FLAG_UTF8: ClassVar[int] = 0x100

    @dataclass(frozen=True)
    class Style:
        """String pool style."""
        spans: Tuple[StringPoolChunk.Span, ...]

        SPAN_END: ClassVar[int] = 0xFFFFFFFF

    # FIXME: show how? use properly!
    @dataclass(frozen=True)
    class Span:
        """String pool style span."""
        name_idx: int
        start: int
        stop: int

    # FIXME: check payload size
    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> StringPoolChunk:
        """Parse StringPoolChunk."""
        d = Chunk._parse(header=header, payload=payload, **kwargs)
        n_strs, n_styles, flags, strs_start, styles_start = struct.unpack("<IIIII", header)
        codec = UTF8 if flags & cls.FLAG_UTF8 else UTF16
        strings = tuple(_read_strings(payload, strs_start - d["header_size"], n_strs, codec))
        styles = tuple(_read_styles(payload, styles_start - d["header_size"], n_styles, n_strs))
        return cls(**d, flags=flags, strings=strings, styles=styles)

    @property
    def is_sorted(self) -> bool:
        """Whether the sorted flag is set."""
        return bool(self.flags & self.FLAG_SORTED)

    @property
    def is_utf8(self) -> bool:
        """Whether the UTF-8 flag is set."""
        return bool(self.flags & self.FLAG_UTF8)

    def string(self, idx: Optional[int]) -> str:
        """Get string by index."""
        return "" if idx is None else self.strings[idx]

    def style(self, idx: int) -> Style:
        """Get style by index."""
        return self.styles[idx]


@dataclass(frozen=True)
class ResourceTableChunk(ParentChunk):
    """Resource table; contains string pool and packages."""
    string_pool: StringPoolChunk = field(repr=False, compare=False)
    packages: Tuple[Tuple[str, PackageChunk], ...] = field(repr=False, compare=False)

    TYPE_ID: ClassVar[int] = 0x0002

    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> ResourceTableChunk:
        """Parse ResourceTableChunk."""
        d = ParentChunk._parse(header=header, payload=payload, **kwargs)
        _size, = struct.unpack("<I", header)
        chunk = cls(**d, string_pool=cast(StringPoolChunk, None), packages=())
        chunk._parse_children(payload)
        string_pool, packages = None, []
        for _, c in chunk.children:
            if isinstance(c, PackageChunk):
                packages.append((c.package_name, c))
            elif isinstance(c, StringPoolChunk):
                if string_pool is not None:
                    raise ParseError("Multiple StringPoolChunk children")
                string_pool = c
        if string_pool is None:
            raise ParseError("No StringPoolChunk child")
        object.__setattr__(chunk, "string_pool", string_pool)
        object.__setattr__(chunk, "packages", tuple(packages))
        return chunk

    @property
    def packages_as_dict(self) -> Dict[str, PackageChunk]:
        """Packages as dict."""
        return dict(self.packages)

    @cached_property
    def _packages_dict(self) -> Dict[str, PackageChunk]:
        return self.packages_as_dict

    def package(self, name: str) -> PackageChunk:
        """Get package by name."""
        return self._packages_dict[name]


@dataclass(frozen=True)
class XMLChunk(ParentChunk):
    """XML chunk; contains string pool and XML nodes."""
    TYPE_ID: ClassVar[int] = 0x0003

    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> XMLChunk:
        """Parse XMLChunk."""
        if header:
            raise ParseError("Expected empty header")
        chunk = cls(**ParentChunk._parse(header=header, payload=payload, **kwargs))
        chunk._parse_children(payload)
        return chunk

    @cached_property
    def string_pool(self) -> StringPoolChunk:
        """Get string pool child."""
        for _, c in self.children:
            if isinstance(c, StringPoolChunk):
                return c
        raise ChildError("No StringPoolChunk child")

    def string(self, idx: Optional[int]) -> str:
        """Get string from string pool by index."""
        return self.string_pool.string(idx)


@dataclass(frozen=True)
class XMLNodeChunk(Chunk):
    """Base class for XML node chunks."""
    lineno: int
    comment_idx: Optional[int]

    @classmethod
    def _parse(_cls, header: bytes, payload: bytes, **kwargs: Any) -> Dict[str, Any]:
        d = Chunk._parse(header=header, payload=payload, **kwargs)
        lineno, comment_idx = struct.unpack("<II", header)
        return dict(**d, lineno=lineno, comment_idx=_noref(comment_idx))

    # FIXME: weakref?
    @cached_property
    def xml_chunk(self) -> XMLChunk:
        """Get XMLChunk parent."""
        r = self.parent
        while r is not None:
            if (p := r()) is None:
                raise ParentError("Parent deallocated")
            if isinstance(p, XMLChunk):
                return p
            r = p.parent
        raise ParentError("No XMLChunk parent")

    def string(self, idx: Optional[int]) -> str:
        """Get string from XML chunk parent by index."""
        return "" if idx is None else self.xml_chunk.string(idx)

    @property
    def comment(self) -> str:
        """Get comment string."""
        return self.string(self.comment_idx)


@dataclass(frozen=True)
class XMLNSChunk(XMLNodeChunk):
    """Base class for XML namespace chunks."""
    prefix_idx: int
    uri_idx: int

    @classmethod
    def _parse(_cls, header: bytes, payload: bytes, **kwargs: Any) -> Dict[str, Any]:
        d = XMLNodeChunk._parse(header=header, payload=payload, **kwargs)
        prefix_idx, uri_idx = struct.unpack("<II", payload)
        return dict(**d, prefix_idx=prefix_idx, uri_idx=uri_idx)

    @property
    def prefix(self) -> str:
        """Get prefix string."""
        return self.string(self.prefix_idx)

    @property
    def uri(self) -> str:
        """Get uri string."""
        return self.string(self.uri_idx)


@dataclass(frozen=True)
class XMLNSStartChunk(XMLNSChunk):
    """XML namespace start."""
    TYPE_ID: ClassVar[int] = 0x0100

    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> XMLNSStartChunk:
        """Parse XMLNSStartChunk."""
        return cls(**XMLNSChunk._parse(header=header, payload=payload, **kwargs))


@dataclass(frozen=True)
class XMLNSEndChunk(XMLNSChunk):
    """XML namespace end."""
    TYPE_ID: ClassVar[int] = 0x0101

    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> XMLNSEndChunk:
        """Parse XMLNSEndChunk."""
        return cls(**XMLNSChunk._parse(header=header, payload=payload, **kwargs))


@dataclass(frozen=True)
class XMLElemStartChunk(XMLNodeChunk):
    """XML element start; contains XML attributes."""
    namespace_idx: Optional[int]
    name_idx: int
    id_idx: Optional[int]
    class_idx: Optional[int]
    style_idx: Optional[int]
    attributes: Tuple[XMLAttr, ...]

    TYPE_ID: ClassVar[int] = 0x0102

    # FIXME: check payload size
    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> XMLElemStartChunk:
        """Parse XMLElemStartChunk."""
        d = XMLNodeChunk._parse(header=header, payload=payload, **kwargs)
        ns_idx, name_idx, attr_start, attr_size, n_attrs, id_idx, class_idx, \
            style_idx, data = _unpack("<IIHHHHHH", payload)
        if attr_size != 20:
            raise ParseError("Wrong XML attribute size")
        # NB: adjust 1-based indices
        chunk = cls(**d, namespace_idx=_noref(ns_idx), name_idx=name_idx,
                    id_idx=_noref(id_idx - 1), class_idx=_noref(class_idx - 1),
                    style_idx=_noref(style_idx - 1), attributes=())
        attrs = tuple(_read_attrs(data, weakref.ref(chunk), attr_start - 20, n_attrs))
        object.__setattr__(chunk, "attributes", attrs)
        return chunk

    @property
    def attrs_as_dict(self) -> Dict[str, XMLAttr]:
        """XML attributes as dict."""
        return {a.name_with_ns: a for a in self.attributes}

    @property
    def namespace(self) -> str:
        """Get namespace string."""
        return self.string(self.namespace_idx)

    @property
    def name(self) -> str:
        """Get name string."""
        return self.string(self.name_idx)


@dataclass(frozen=True)
class XMLElemEndChunk(XMLNodeChunk):
    """XML element end."""
    namespace_idx: Optional[int]
    name_idx: int

    TYPE_ID: ClassVar[int] = 0x0103

    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> XMLElemEndChunk:
        """Parse XMLElemEndChunk."""
        d = XMLNodeChunk._parse(header=header, payload=payload, **kwargs)
        namespace_idx, name_idx = struct.unpack("<II", payload)
        return cls(**d, namespace_idx=_noref(namespace_idx), name_idx=name_idx)

    @property
    def namespace(self) -> str:
        """Get namespace string."""
        return self.string(self.namespace_idx)

    @property
    def name(self) -> str:
        """Get name string."""
        return self.string(self.name_idx)


@dataclass(frozen=True)
class XMLCDATAChunk(XMLNodeChunk):
    """XML CDATA."""
    raw_value_idx: int
    typed_value: BinResVal

    TYPE_ID: ClassVar[int] = 0x0104

    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> XMLCDATAChunk:
        """Parse XMLCDATAChunk."""
        d = XMLNodeChunk._parse(header=header, payload=payload, **kwargs)
        raw_value_idx, tv_data = _unpack("<I", payload)
        return cls(**d, raw_value_idx=raw_value_idx, typed_value=_read_brv(tv_data))

    @property
    def raw_value(self) -> str:
        """Get raw value string."""
        return self.string(self.raw_value_idx)


@dataclass(frozen=True)
class XMLResourceMapChunk(Chunk):
    """XML resource map."""
    resources: Tuple[int, ...]

    TYPE_ID: ClassVar[int] = 0x0180

    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> XMLResourceMapChunk:
        """Parse XMLResourceMapChunk."""
        d = Chunk._parse(header=header, payload=payload, **kwargs)
        n = (d["chunk_size"] - d["header_size"]) // 4
        return cls(**d, resources=struct.unpack(f"<{n}I", payload))

    def resource(self, i: int) -> BinResId:
        """Get resource by index."""
        return BinResId.from_int(self.resources[i])


@dataclass(frozen=True)
class PackageChunk(ParentChunk):
    """
    Package chunk; contains type specs and types.

    NB: types can have multiple values for the same key.
    """
    id: int
    package_name: str
    type_specs: Tuple[Tuple[int, TypeSpecChunk], ...] = field(repr=False, compare=False)
    types: Tuple[Tuple[int, TypeChunk], ...] = field(repr=False, compare=False)
    library_chunk: Optional[LibraryChunk] = field(repr=False, compare=False)
    _type_strings_offset: int = field(repr=False, compare=False)
    _key_strings_offset: int = field(repr=False, compare=False)

    TYPE_ID: ClassVar[int] = 0x0200

    # NB: last public type/key offset in string pool & type id offset are unused
    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> PackageChunk:
        """Parse PackageChunk."""
        d = ParentChunk._parse(header=header, payload=payload, **kwargs)
        id_, name_b, t_off, last_pub_t, k_off, last_pub_k, tid_off = \
            struct.unpack("<I256sIIIII", header)
        name = _decode_package_name(name_b)
        chunk = cls(**d, id=id_, package_name=name, type_specs=(), types=(),
                    library_chunk=None, _key_strings_offset=k_off, _type_strings_offset=t_off)
        chunk._parse_children(payload)
        type_specs, types, library_chunk = [], [], None
        for _, c in chunk.children:
            if isinstance(c, TypeSpecChunk):
                type_specs.append((c.id, c))
            elif isinstance(c, TypeChunk):
                types.append((c.id, c))
            elif isinstance(c, LibraryChunk):
                if library_chunk is not None:
                    raise ParseError("Multiple LibraryChunk children")
                library_chunk = c
            elif not isinstance(c, StringPoolChunk):
                raise ParseError(f"Unexpected {c.__class__.__name__} child")
        object.__setattr__(chunk, "type_specs", tuple(type_specs))
        object.__setattr__(chunk, "types", tuple(types))
        object.__setattr__(chunk, "library_chunk", library_chunk)
        return chunk

    @property
    def type_specs_as_dict(self) -> Dict[int, TypeSpecChunk]:
        """Type specs as dict."""
        return dict(self.type_specs)

    @property
    def types_as_dict(self) -> Dict[int, List[TypeChunk]]:
        """Types as dict of lists."""
        d: Dict[int, List[TypeChunk]] = {}
        for i, c in self.types:
            d.setdefault(i, []).append(c)
        return d

    @cached_property
    def _type_specs_dict(self) -> Dict[int, TypeSpecChunk]:
        return self.type_specs_as_dict

    @cached_property
    def _types_dict(self) -> Dict[int, List[TypeChunk]]:
        return self.types_as_dict

    def type_spec_chunks(self) -> Tuple[TypeSpecChunk, ...]:
        """Get all type specs."""
        return tuple(c for _, c in self.type_specs)

    def type_spec_chunk(self, type_id: Union[int, str]) -> TypeSpecChunk:
        """Get type spec by id"""
        if isinstance(type_id, str):
            type_id = self.type_string_pool.strings.index(type_id) + 1
        return self._type_specs_dict[type_id]

    def type_chunks(self, type_id: Union[int, str, None]) -> Tuple[TypeChunk, ...]:
        """Get all types or types by id."""
        if type_id is None:
            return tuple(c for _, c in self.types)
        if isinstance(type_id, str):
            type_id = self.type_string_pool.strings.index(type_id) + 1
        return tuple(self._types_dict[type_id])

    @cached_property
    def type_string_pool(self) -> StringPoolChunk:
        """Get type string pool child."""
        return self._string_pool(self._type_strings_offset, "type")

    @cached_property
    def key_string_pool(self) -> StringPoolChunk:
        """Get key string pool child."""
        return self._string_pool(self._key_strings_offset, "key")

    def _string_pool(self, offset: int, what: str) -> StringPoolChunk:
        pool = None
        for o, c in self.children:
            if o == offset:
                pool = c
                break
        if not isinstance(pool, StringPoolChunk):
            raise ChildError(f"Unable to find {what} string pool")
        return pool


@dataclass(frozen=True)
class TypeOrSpecChunk(Chunk):
    """Base class for TypeChunk and TypeSpecChunk."""
    id: int

    @classmethod
    def _parse(_cls, header: bytes, payload: bytes, **kwargs: Any) -> Dict[str, Any]:
        return Chunk._parse(header=header, payload=payload, **kwargs)

    # FIXME: weakref?
    @cached_property
    def package_chunk(self) -> Optional[PackageChunk]:
        """Get PackageChunk parent."""
        r = self.parent
        while r is not None:
            if (p := r()) is None:
                raise ParentError("Parent deallocated")
            if isinstance(p, PackageChunk):
                return p
            r = p.parent
        return None

    @property
    def type_name(self) -> str:
        """Get type name from package chunk type string pool."""
        if (c := self.package_chunk) is None:
            raise ParentError("No PackageChunk parent")
        return c.type_string_pool.strings[self.id - 1]

    def resource_id(self, entry_id: int) -> BinResId:
        """Get resource ID (package + type + entry) for entry."""
        if (c := self.package_chunk) is None:
            raise ParentError("No PackageChunk parent")
        return BinResId(c.id, self.id, entry_id)


# FIXME: incomplete
@dataclass(frozen=True)
class TypeChunk(TypeOrSpecChunk):
    """Type chunk; contains entries and configuration."""
    entries: Tuple[Tuple[int, Entry], ...]
    configuration: BinResCfg

    TYPE_ID: ClassVar[int] = 0x0201

    NO_ENTRY: ClassVar[int] = 0xFFFFFFFF

    @dataclass(frozen=True)
    class Entry:
        """Type chunk entry."""
        header_size: int
        flags: int
        key_index: int
        value: Optional[BinResVal]
        values: Tuple[Tuple[int, BinResVal], ...]
        parent_entry: int
        parent: TypeChunkRef = field(repr=False, compare=False)

        FLAG_COMPLEX: ClassVar[int] = 0x1

        @property
        def values_as_dict(self) -> Dict[int, BinResVal]:
            """Values as dict."""
            return dict(self.values)

        @cached_property
        def _values_dict(self) -> Dict[int, BinResVal]:
            return self.values_as_dict

        @property
        def is_complex(self) -> bool:
            """Whether the entry has multiple values (instead of one value)."""
            return bool(self.flags & self.FLAG_COMPLEX)

        @property
        def key(self) -> str:
            """Get key name from TypeChunk parent."""
            if (p := self.parent()) is not None:
                return p.key_name(self.key_index)
            raise ParentError("Parent deallocated")

    # FIXME: configuration
    # FIXME: check payload size
    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> TypeChunk:
        """Parse TypeChunk."""
        d = TypeOrSpecChunk._parse(header=header, payload=payload, **kwargs)
        id_, n_ents, start, cfg_data = _unpack("<III", header)
        cfg = BinResCfg()
        chunk = cls(**d, id=id_, entries=(), configuration=cfg)
        entries = []
        for i in range(n_ents):
            off, = struct.unpack("<I", payload[4 * i:4 * (i + 1)])
            if off == cls.NO_ENTRY:
                continue
            o = off + start - d["header_size"]
            hdr_sz, flags, key_idx = struct.unpack("<HHI", payload[o:o + 8])
            values = []
            if flags & cls.Entry.FLAG_COMPLEX:
                value = None
                par_ent, n = struct.unpack("<II", payload[o + 8:o + 16])
                for j in range(n):
                    data = payload[o + 16 + 12 * j:o + 16 + 12 * (j + 1)]
                    k, brv_data = _unpack("<I", data)
                    values.append((k, _read_brv(brv_data)))
            else:
                value = _read_brv(payload[o + 8:o + 16])
                par_ent = 0
            e = cls.Entry(header_size=hdr_sz, flags=flags, key_index=key_idx,
                          value=value, values=tuple(values), parent_entry=par_ent,
                          parent=weakref.ref(chunk))
            entries.append((i, e))
        object.__setattr__(chunk, "entries", tuple(entries))
        return chunk

    @property
    def entries_as_dict(self) -> Dict[int, Entry]:
        """Entries as dict."""
        return dict(self.entries)

    @cached_property
    def _entries_dict(self) -> Dict[int, Entry]:
        return self.entries_as_dict

    # FIXME: weakref?
    @cached_property
    def resource_table_chunk(self) -> Optional[ResourceTableChunk]:
        """Get ResourceTableChunk parent."""
        r = self.parent
        while r is not None:
            if (p := r()) is None:
                raise ParentError("Parent deallocated")
            if isinstance(p, ResourceTableChunk):
                return p
            r = p.parent
        return None

    def string(self, idx: Optional[int]) -> str:
        """Get string from resource table parent string pool by index."""
        if (c := self.resource_table_chunk) is None:
            raise ParentError("No ResourceTableChunk parent")
        return c.string_pool.string(idx)

    def key_name(self, idx: Optional[int]) -> str:
        """Get key from package chunk parent key string pool by index."""
        if (c := self.package_chunk) is None:
            raise ParentError("No PackageChunk parent")
        return c.key_string_pool.string(idx)

    def contains_resource(self, rid: BinResId) -> bool:
        """Whether the entries contain the resource."""
        if (c := self.package_chunk) is None:
            raise ParentError("No PackageChunk parent")
        return rid.package_id == c.id and rid.type_id == self.id \
            and rid.entry_id in self._entries_dict


@dataclass(frozen=True)
class TypeSpecChunk(TypeOrSpecChunk):
    """Type spec chunk."""
    resources: Tuple[int, ...]

    TYPE_ID: ClassVar[int] = 0x0202

    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> TypeSpecChunk:
        "Parse TypeSpecChunk."""
        d = TypeOrSpecChunk._parse(header=header, payload=payload, **kwargs)
        id_, n = struct.unpack("<II", header)
        return cls(**d, id=id_, resources=struct.unpack(f"<{n}I", payload))


# FIXME: untested!
@dataclass(frozen=True)
class LibraryChunk(Chunk):
    """Library chunk."""
    entries: Tuple[Entry, ...]

    TYPE_ID: ClassVar[int] = 0x0203

    @dataclass(frozen=True)
    class Entry:
        """Library chunk entry."""
        id: int
        package_name: str

    @classmethod
    def parse(cls, header: bytes, payload: bytes, **kwargs: Any) -> LibraryChunk:
        """Parse LibraryChunk."""
        d = Chunk._parse(header=header, payload=payload, **kwargs)
        n, = _unpack("<I", header)
        if len(payload) != 256 * n:
            raise ParseError(f"Payload size mismatch: expected {256 * n}, got {len(payload)}")
        entries = []
        for i in range(n):
            id_, name_b, payload = _unpack("<I256s", payload)
            entries.append(cls.Entry(id_, _decode_package_name(name_b)))
        return cls(**d, entries=tuple(entries))


@dataclass(frozen=True)
class UnknownChunk(Chunk):
    """Unknown chunk."""
    header: bytes = field(repr=False)
    payload: bytes = field(repr=False)
    _type_id: int

    @property
    def type_id(self) -> int:
        return self._type_id


@dataclass(frozen=True)
class BinResVal:
    """Binary resource value."""
    size: int
    type: Type
    data: int

    COMPLEX_UNITS: ClassVar[Tuple[str, ...]] = ("px", "dp", "sp", "pt", "in", "mm")
    COMPLEX_UNIT_FRACTIONS: ClassVar[Tuple[str, ...]] = ("%", "%p")
    COMPLEX_UNIT_MASK: ClassVar[int] = 0xf

    COMPLEX_RADIX_MASK: ClassVar[int] = 0x3
    COMPLEX_RADIX_SHIFT: ClassVar[int] = 4
    COMPLEX_RADIX_SHIFTS: ClassVar[Tuple[int, ...]] = (23, 16, 8, 0)

    COMPLEX_MANTISSA_MASK: ClassVar[int] = 0xffffff
    COMPLEX_MANTISSA_SHIFT: ClassVar[int] = 8

    class Type(Enum):
        """Binary resource value type."""
        NULL = 0x00                 # 0 = undef, 1 = empty
        REFERENCE = 0x01            # reference to resource table entry
        ATTRIBUTE = 0x02            # attribute resource identifier
        STRING = 0x03               # string pool index
        FLOAT = 0x04
        DIMENSION = 0x05            # see complex2pair
        FRACTION = 0x06             # see complex2pair
        DYNAMIC_REFERENCE = 0x07
        DYNAMIC_ATTRIBUTE = 0x08
        INT_DEC = 0x10
        INT_HEX = 0x11
        INT_BOOLEAN = 0x12
        INT_COLOR_ARGB8 = 0x1c      # #aarrggbb
        INT_COLOR_RGB8 = 0x1d       # #rrggbb
        INT_COLOR_ARGB4 = 0x1e      # #argb
        INT_COLOR_RGB4 = 0x1f       # #rgb

    @classmethod
    def complex2pair(c, i: int, *, fraction: bool) -> Tuple[float, str]:
        """Parse dimension/fraction to (float, suffix)."""
        unt = i & c.COMPLEX_UNIT_MASK
        rad = (i >> c.COMPLEX_RADIX_SHIFT) & c.COMPLEX_RADIX_MASK
        man = ((i >> c.COMPLEX_MANTISSA_SHIFT) & c.COMPLEX_MANTISSA_MASK) \
            << c.COMPLEX_RADIX_SHIFTS[rad]
        val = man * (1.0 / (1 << 23))
        suf = c.COMPLEX_UNIT_FRACTIONS[unt] if fraction else c.COMPLEX_UNITS[unt]
        return val, suf


@dataclass(frozen=True)
class BinResId:
    """Binary resource ID."""
    package_id: int
    type_id: int
    entry_id: int

    @classmethod
    def from_int(cls, i: int) -> BinResId:
        """Convert from int."""
        p = (i & 0xFF000000) >> 24
        t = (i & 0x00FF0000) >> 16
        e = (i & 0x0000FFFF)
        return cls(package_id=p, type_id=t, entry_id=e)

    @property
    def to_int(self) -> int:
        """Convert to int."""
        return self.package_id << 24 | self.type_id << 16 | self.entry_id


# FIXME: incomplete
@dataclass(frozen=True)
class BinResCfg:
    """Binary resource configuration."""


@dataclass(frozen=True)
class XMLAttr:
    """XML attribute; contains typed and/or raw value."""
    namespace_idx: Optional[int]
    name_idx: int
    raw_value_idx: Optional[int]
    typed_value: BinResVal
    parent: XMLNodeChunkRef = field(repr=False, compare=False)

    def string(self, idx: Optional[int]) -> str:
        """Get string from XML node parent by index."""
        if idx is None:
            return ""
        if (p := self.parent()) is not None:
            return p.string(idx)
        raise ParentError("Parent deallocated")

    @property
    def namespace(self) -> str:
        """Get namespace string."""
        return self.string(self.namespace_idx)

    @property
    def name(self) -> str:
        """Get name string."""
        return self.string(self.name_idx)

    @property
    def name_with_ns(self) -> str:
        """Get name with namespace (if any) prepended; e.g. '{{foo}}bar'."""
        return f"{{{self.namespace}}}{self.name}" if self.namespace else self.name

    @property
    def raw_value(self) -> str:
        """Get raw value string."""
        return self.string(self.raw_value_idx)


if TYPE_CHECKING:
    ChunkRef = weakref.ReferenceType[Chunk]
    XMLNodeChunkRef = weakref.ReferenceType[XMLNodeChunk]
    TypeChunkRef = weakref.ReferenceType[TypeChunk]
else:
    ChunkRef = weakref.ReferenceType
    XMLNodeChunkRef = weakref.ReferenceType
    TypeChunkRef = weakref.ReferenceType


def _subclasses(cls: Any) -> Iterator[Any]:
    for c in cls.__subclasses__():
        yield c
        yield from _subclasses(c)


CHUNK_TYPES = {c.TYPE_ID: c for c in _subclasses(Chunk) if c.TYPE_ID is not None}

HIDDEN_FIELDS = {"chunk_size", "header_size", "level", "parent", "offset", "typed_value"}
VERBOSE_FIELDS = {"comment_idx"}


# FIXME
def dump(*files: str, json: bool = False, verbose: bool = False,
         xml: bool = False) -> None:
    """Parse AXML/ARSC & dump to stdout."""
    one = len(files) == 1
    for file in files:
        with open(file, "rb") as fh:
            if not (one or json or xml):
                print(f"file={file!r}")
            _dump(fh.read(), json=json, verbose=verbose, xml=xml)


# FIXME
def dump_apk(apk: str, *patterns: str, json: bool = False,
             verbose: bool = False, xml: bool = False) -> None:
    """Parse AXML/ARSC in APK & dump to stdout."""
    with zipfile.ZipFile(apk) as zf:
        for info in zf.infolist():
            if fnmatches_with_negation(info.filename, *patterns):
                if not (json or xml):
                    print(f"entry={info.filename!r}")
                with zf.open(info.filename) as fh:
                    _dump(fh.read(), json=json, verbose=verbose, xml=xml)


def fastid(*apks: str, json: bool = False) -> None:
    """Quickly get appid & version code/name from APK & print to stdout."""
    if json:
        result = []
        for apk in apks:
            appid, vercode, vername = quick_get_appid_version(apk)
            result.append(dict(package=appid, versionCode=vercode, versionName=vername))
        print(_json.dumps(result, indent=2))
    else:
        for apk in apks:
            print(*quick_get_appid_version(apk))


def _dump(data: bytes, *, json: bool, verbose: bool, xml: bool) -> None:
    magic = data[:4]
    if magic == ARSC_MAGIC:
        if xml:
            raise Error("ARSC does not contain XML")
        dump_arsc(*parse(data), json=json, verbose=verbose)
    elif magic == AXML_MAGIC:
        dump_axml(*parse(data), json=json, verbose=verbose, xml=xml)
    else:
        raise Error(f"Unsupported magic {magic!r}")


# FIXME
def dump_arsc(*chunks: Chunk, json: bool = False, verbose: bool = False) -> None:
    """Dump ARSC chunks to stdout."""
    if json:
        show_json(*chunks)
    else:
        show_chunks(*chunks, verbose=verbose)


# FIXME
def dump_axml(*chunks: Chunk, json: bool = False, verbose: bool = False,
              xml: bool = False) -> None:
    """Dump AXML chunks to stdout."""
    if json:
        show_json(*chunks)
    elif xml:
        show_xml(*chunks)
    else:
        show_chunks(*chunks, verbose=verbose)


# FIXME
def show_chunks(*chunks: Chunk, file: Optional[TextIO] = None, verbose: bool) -> None:
    """Show AXML/ARSC chunks as parse tree."""
    if file is None:
        file = sys.stdout
    for chunk in chunks:
        idt, name = "  " * chunk.level, _clsname(chunk.__class__)
        fs, sub = [], []
        for f in dataclasses.fields(chunk):
            k = f.name
            hid, ver = k in HIDDEN_FIELDS, k in VERBOSE_FIELDS
            if not f.repr or hid or (not verbose and ver):
                continue
            if k.endswith("_idx"):
                if not hasattr(chunk, k[:-4]):
                    continue
                k = k[:-4]
            v = getattr(chunk, k)
            if v == "":
                pass
            elif isinstance(v, tuple):
                if verbose or k in ("attributes", "entries"):
                    if v:
                        sub.append((k, v))
                else:
                    fs.append((f"#{k}", len(v)))
            elif ver:
                sub.append((k, v))
            else:
                fs.append((k, v))
        print(f"{idt}{name}{_fs_info(fs)}", file=file)
        for k, v in sub:
            if isinstance(v, tuple):
                if k == "attributes":
                    for x in v:
                        show_xml_attr(x, f"{idt}  ATTR: ", file=file)
                elif k == "entries":
                    if isinstance(chunk, TypeChunk):
                        for i, x in v:
                            show_type_entry(chunk, i, x, f"{idt}  ", file=file)
                    else:
                        # FIXME: LibraryChunk
                        raise NotImplementedError("FIXME")
                else:
                    print(f"{idt}  {k.upper()}:", file=file)
                    for x in v:
                        if isinstance(x, StringPoolChunk.Style):
                            spans = ", ".join(str(dataclasses.astuple(s)) for s in x.spans)
                            y = f"SPANS: {spans}"
                        else:
                            y = hex(x) if isinstance(x, int) else repr(x)
                        print(f"{idt}    {y}", file=file)
            else:
                print(f"{idt}  {k.upper()}: {v!r}", file=file)
        if hasattr(chunk, "children"):
            show_chunks(*(c for _, c in chunk.children), file=file, verbose=verbose)


def _fs_info(fs: List[Tuple[str, Any]]) -> str:
    fs_joined = ", ".join(f"{k}={hex(v) if k == 'id' else repr(v)}" for k, v in fs)
    return f" [{fs_joined}]" if fs else ""


# FIXME
# FIXME: LibraryChunk
def show_json(*chunks: Chunk, file: Optional[TextIO] = None) -> None:
    """Show AXML/ARSC chunks as JSON."""
    def for_json(obj: Any) -> Any:
        if isinstance(obj, (Chunk, XMLAttr, BinResVal, StringPoolChunk.Style,
                            TypeChunk.Entry)):
            d: Dict[str, Any] = dict(_type=obj.__class__.__name__)
            for f in dataclasses.fields(obj):
                k = f.name
                if k == "_type_id":
                    k = k[1:]
                elif k == "parent" or k.startswith("_"):
                    continue
                if k.endswith("_idx") and hasattr(obj, k[:-4]):
                    k = k[:-4]
                v = getattr(obj, k)
                if k == "children":
                    d[k] = [for_json(c) for _, c in v]
                elif k in ("attributes", "styles", "spans", "values"):
                    d[k] = [for_json(c) for c in v]
                elif k in ("packages", "types", "type_specs", "entries"):
                    d[k] = [(x, for_json(c)) for x, c in v]
                elif k in ("type", "typed_value", "string_pool", "library_chunk", "value"):
                    d[k] = for_json(v) if v is not None else None
                else:
                    d[k] = v
            return d
        if isinstance(obj, BinResVal.Type):
            return dict(name=obj.name, value=obj.value)
        if isinstance(obj, StringPoolChunk.Span):
            return dataclasses.astuple(obj)
        raise TypeError(f"Unserializable {obj.__class__.__name__}")
    if file is None:
        file = sys.stdout
    _json.dump([for_json(c) for c in chunks], file, indent=2, sort_keys=True)
    print(file=file)


# FIXME
def show_xml(*chunks: Chunk, file: Optional[TextIO] = None) -> None:
    """Show AXML chunks as XML."""
    import xml.etree.ElementTree as ET

    def indent(root: ET.Element) -> None:
        def _indent_children(elem: ET.Element, level: int) -> None:
            if not elem.text or not elem.text.strip():
                elem.text = "\n" + "  " * (level + 1)
            for child in elem:
                if len(child):
                    _indent_children(child, level + 1)
                if not child.tail or not child.tail.strip():
                    last_tail = child.tail = "\n" + "  " * (level + 1)
                else:
                    last_tail = None
            if child.tail and last_tail is not None:
                child.tail = child.tail[:-2]
        if len(root):
            _indent_children(root, 0)

    if file is None:
        file = sys.stdout
    bio = io.BytesIO()
    found = False
    for chunk in chunks:
        if isinstance(chunk, XMLChunk):
            found = True
            nsmap = ET._namespace_map.copy()    # type: ignore[attr-defined]
            try:
                tb = ET.TreeBuilder()
                for _, c in chunk.children:
                    if isinstance(c, XMLNSStartChunk):
                        ET.register_namespace(c.prefix, c.uri)
                    elif isinstance(c, XMLElemStartChunk):
                        attrs = {}
                        for a in c.attributes:
                            attrs[a.name_with_ns] = brv_str(a.typed_value, a.raw_value)
                        tb.start(c.name, attrs)
                    elif isinstance(c, XMLElemEndChunk):
                        tb.end(c.name)
                    elif isinstance(c, XMLCDATAChunk):
                        tb.data(brv_str(c.typed_value, c.raw_value))
                tree = ET.ElementTree(tb.close())
                indent(tree.getroot())
                tree.write(bio)
            finally:
                ET._namespace_map = nsmap       # type: ignore[attr-defined]
            print(bio.getvalue().decode(), file=file)
    if not found:
        raise Error("No XML chunks")


def show_xml_attr(attr: XMLAttr, pre: str = "", *,
                  file: Optional[TextIO] = None) -> None:
    """Show XMLAttr."""
    if file is None:
        file = sys.stdout
    ns, name, tv = attr.namespace, attr.name, attr.typed_value
    ns_info = f"{repr(ns)[1:-1]}:" if ns else ""
    v = brv_repr(tv, attr.raw_value)
    print(f"{pre}{ns_info}{repr(name)[1:-1]}={v}", file=file)


def show_type_entry(c: TypeChunk, i: int, e: TypeChunk.Entry,
                    pre: str = "", *, file: Optional[TextIO] = None) -> None:
    """Show TypeChunk.Entry."""
    if file is None:
        file = sys.stdout
    info = f"id=0x{c.resource_id(i).to_int:08x}, key={e.key!r}"
    if e.parent_entry:
        info += f", parent=0x{e.parent_entry:08x}"
    print(f"{pre}ENTRY [{info}]", file=file)
    values: Tuple[Tuple[Optional[int], BinResVal], ...]
    if e.is_complex:
        values = e.values
    else:
        assert e.value is not None
        values = ((None, e.value),)
    for k, brv in values:
        r = c.string(brv.data) if brv.type is BinResVal.Type.STRING else ""
        v = brv_repr(brv, r)
        print(f"{pre}  VALUE{'' if k is None else f' 0x{k:08x}'}: {v}")


def brv_repr(brv: BinResVal, raw_value: str) -> str:
    """repr() for BinResVal."""
    f_repr, _, x = brv_to_py(brv, raw_value)
    return f_repr(x)


def brv_str(brv: BinResVal, raw_value: str) -> str:
    """str() for BinResVal."""
    _, f_str, x = brv_to_py(brv, raw_value)
    return f_str(x)


# FIXME: incomplete
def brv_to_py(brv: BinResVal, raw_value: str) \
        -> Tuple[Callable[[Any], str], Callable[[Any], str], Any]:
    """Convert BinResVal to value + repr/str functions to show it."""
    def null2s(i: int) -> str:
        return "@empty" if i == 1 else "@null"

    def ref2s(i: int) -> str:
        return f"@{i2h(i)}"

    def attr2s(i: int) -> str:
        return f"?{i2h(i)}"

    def f2s(f: float) -> str:
        return f"{f:g}"

    def c2s(c: Tuple[float, str]) -> str:
        val, suf = c
        return f"{val:f}{suf}"

    def i2h(i: int) -> str:
        return f"0x{i:08x}"

    def b2s(b: bool) -> str:
        return str(b).lower()

    def clr2s(i: int) -> str:
        return f"#{i:08x}"

    t, T = brv.type, BinResVal.Type
    if t is T.NULL:
        if brv.size == 0 and raw_value:
            return repr, str, raw_value
        return null2s, null2s, brv.data
    elif t is T.REFERENCE:
        return ref2s, ref2s, brv.data
    elif t is T.ATTRIBUTE:
        return attr2s, attr2s, brv.data
    elif t is T.STRING:
        return repr, str, raw_value
    elif t is T.FLOAT:
        return f2s, f2s, struct.unpack("<f", struct.pack("<I", brv.data))[0]
    elif t is T.DIMENSION:
        return c2s, c2s, BinResVal.complex2pair(brv.data, fraction=False)
    elif t is T.FRACTION:
        return c2s, c2s, BinResVal.complex2pair(brv.data, fraction=True)
    elif t in (T.DYNAMIC_REFERENCE, T.DYNAMIC_ATTRIBUTE):
        raise NotImplementedError("Dynamic reference/attribute not (yet) supported")
    elif t is T.INT_DEC:
        return str, str, brv.data
    elif t is T.INT_HEX:
        return i2h, i2h, brv.data
    elif t is T.INT_BOOLEAN:
        return b2s, b2s, (False if brv.data == 0 else True)
    elif t in (T.INT_COLOR_ARGB8, T.INT_COLOR_RGB8, T.INT_COLOR_ARGB4, T.INT_COLOR_RGB4):
        return clr2s, clr2s, brv.data
    else:
        raise ValueError(f"Unsupported value type {t.name}")


def parse(data: bytes) -> Tuple[Chunk, ...]:
    """Parse raw data to AXML/ARSC chunks."""
    return tuple(c for _, c in read_chunks(data))


def read_chunks(data: bytes, parent: Optional[ChunkRef] = None, level: int = 0,
                offset: int = 0) -> Iterator[Tuple[int, Chunk]]:
    """Read multiple chunks (+ offsets)."""
    while len(data) >= 8:
        chunk, data, level = read_chunk(data, parent, level, offset)
        yield offset, chunk
        offset += chunk.chunk_size
    if data:
        raise ParseError("Expected end of data")


def read_chunk(data: bytes, parent: Optional[ChunkRef] = None,
               level: int = 0, offset: int = 0) -> Tuple[Chunk, bytes, int]:
    """Read one chunk; returns chunk, remainder, level."""
    type_id, d, data = _read_chunk(data, parent)
    if ct := CHUNK_TYPES.get(type_id):
        if "End" in ct.__name__:
            level -= 1
        chunk = ct.parse(**d, level=level, offset=offset)
        if "Start" in ct.__name__:
            level += 1
    else:
        kw = {**d, **Chunk._parse(**d)}
        chunk = UnknownChunk(_type_id=type_id, level=level, offset=offset, **kw)
    return chunk, data, level


def _read_chunk(data: bytes, parent: Optional[ChunkRef] = None) -> Tuple[int, Dict[str, Any], bytes]:
    type_id, header_size, chunk_size, data = _unpack("<HHI", data)
    if header_size > chunk_size:
        raise ParseError("Header size > chunk size")
    if chunk_size - 8 > len(data):
        raise ParseError("Not enough data for chunk")
    header, data = _split(data, header_size - 8)
    chunk_data, data = _split(data, chunk_size - header_size)
    d = dict(header=header, payload=chunk_data, parent=parent)
    return type_id, d, data


def _unpack(fmt: str, data: bytes) -> Any:
    assert all(c in "<BHI" for c in fmt)
    size = fmt.count("B") + 2 * fmt.count("H") + 4 * fmt.count("I")
    return struct.unpack(fmt, data[:size]) + (data[size:],)


def _split(data: bytes, size: int) -> Tuple[bytes, bytes]:
    return data[:size], data[size:]


def _noref(idx: int) -> Optional[int]:
    return None if idx in (-1, -1 & 0xFFFFFFFF) else idx


def _read_attrs(data: bytes, parent: XMLNodeChunkRef,
                start: int, n: int) -> Iterator[XMLAttr]:
    for i in range(n):
        attr_data = data[start + 20 * i:start + 20 * (i + 1)]
        ns_idx, name_idx, raw_value_idx, tv_data = _unpack("<III", attr_data)
        yield XMLAttr(_noref(ns_idx), name_idx, _noref(raw_value_idx),
                      _read_brv(tv_data), parent)


def _read_brv(data: bytes) -> BinResVal:
    size, _, typ = struct.unpack("<HBB", data[:4])
    i, = struct.unpack("<i" if typ == BinResVal.Type.INT_DEC.value else "<I", data[4:])
    return BinResVal(size, BinResVal.Type(typ), i)


def _read_strings(data: bytes, off: int, n: int, codec: str) -> Iterator[str]:
    for i in range(n):
        o, = struct.unpack("<I", data[4 * i:4 * (i + 1)])
        yield _decode_string(data, off + o, codec)


def _read_styles(data: bytes, off: int, n: int, m: int) -> Iterator[StringPoolChunk.Style]:
    for i in range(n):
        o, = struct.unpack("<I", data[4 * (m + i):4 * (m + i + 1)])
        yield StringPoolChunk.Style(tuple(_read_spans(data, off + o)))


def _read_spans(data: bytes, off: int) -> Iterator[StringPoolChunk.Span]:
    while True:
        name_idx, = struct.unpack("<I", data[off:off + 4])
        if name_idx == StringPoolChunk.Style.SPAN_END:
            break
        start, stop = struct.unpack("<II", data[off + 4:off + 12])
        yield StringPoolChunk.Span(name_idx, start, stop)
        off += 12


def _decode_string(data: bytes, off: int, codec: str) -> str:
    if codec == UTF8:
        i, m = _decode_length(data, off, codec)
        j, n = _decode_length(data, off + i, codec)
        a, b = off + i + j, off + i + j + n
        try:
            s = data[a:b].decode(codec)
            k = len(s)
        except UnicodeDecodeError:
            k, s = _decode_utf8_with_surrogates(data[a:b])
        if k != m:
            log = logging.getLogger(__name__)
            log.warning(f"UTF-8 string length mismatch: expected {m}, got {k}")
        if data[b] != 0:
            raise ParseError("UTF-8 string not null-terminated")
    elif codec == UTF16:
        i, n = _decode_length(data, off, codec)
        a, b = off + i, off + i + 2 * n
        s = data[a:b].decode(codec)
        if data[b:b + 2] != b"\x00\x00":
            raise ParseError("UTF-16 string not null-terminated")
    else:
        raise ValueError(f"Unsupported codec {codec!r}")
    return s


def _decode_utf8_with_surrogates(b: bytes) -> Tuple[int, str]:
    s = b.decode(UTF8, errors="surrogatepass")
    i, n, t = 0, len(s), []
    while i < n:
        if i != n - 1:
            o1, o2 = ord(s[i]), ord(s[i + 1])
            if 0xd800 <= o1 <= 0xdbff and 0xdc00 <= o2 <= 0xdfff:
                t.append(chr(0x10000 + (((o1 - 0xd800) << 10) | (o2 - 0xdc00))))
                i += 2
                continue
        t.append(s[i])
        i += 1
    return n, "".join(t)


def _decode_length(data: bytes, off: int, codec: str) -> Tuple[int, int]:
    if codec == UTF8:
        i, n = 1, data[off]
        if n & 0x80:
            i, n = 2, (n & 0x7F) << 8 | data[off + 1]
    elif codec == UTF16:
        i, n = 2, int.from_bytes(data[off:off + 2], "little")
        if n & 0x8000:
            i, n = 4, (n & 0x7FFF) << 16 | int.from_bytes(data[off + 2:off + 4], "little")
    else:
        raise ValueError(f"Unsupported codec {codec!r}")
    return i, n


def _decode_package_name(b: bytes) -> str:
    i = -1
    while (i := b.index(b"\x00\x00", i + 1)) % 2:
        pass
    return b[:i].decode(UTF16)


def _clsname(cls: type) -> str:
    name = cls.__name__.replace("Chunk", "").replace("XML", "XML ")
    name_sp = re.sub(r"([A-Z][a-z])", r" \1", name)
    return " ".join(x.upper() for x in name_sp.split())


# FIXME
def quick_get_appid_version(apk: str) -> Tuple[str, int, str]:
    """Quickly get appid & version code/name from APK."""
    tid, d, _ = _read_chunk(quick_load(apk, MANIFEST))
    if tid != XMLChunk.TYPE_ID:
        raise ParseError("Expected XMLChunk")
    data, d["payload"] = d["payload"], b""
    xml = XMLChunk.parse(**d, level=0, offset=-1)
    ref: Optional[ChunkRef] = weakref.ref(xml)
    pool = start = None
    while data:
        tid, d, data = _read_chunk(data, parent=ref)
        if tid == StringPoolChunk.TYPE_ID:
            pool = StringPoolChunk.parse(**d, level=0, offset=-1)
        elif tid == XMLElemStartChunk.TYPE_ID:
            start = XMLElemStartChunk.parse(**d, level=0, offset=-1)
        if pool and start:
            break
    else:
        raise ParseError("Expected StringPoolChunk and XMLElemStartChunk")
    object.__setattr__(xml, "children", ((-1, pool), (-1, start)))
    if start.name != "manifest":
        raise ParseError("Expected manifest element")
    appid = vercode = vername = None
    for a in start.attributes:
        if a.name == "package" and not a.namespace:
            appid = a.raw_value
        elif a.name == "versionCode" and a.namespace == SCHEMA_ANDROID:
            vercode = a.typed_value.data
        elif a.name == "versionName" and a.namespace == SCHEMA_ANDROID:
            vername = a.raw_value
        if appid is not None and vercode is not None and vername is not None:
            break
    else:
        raise ParseError("Could not find required attribute(s)")
    return appid, vercode, vername


def quick_load(apk: str, filename: str) -> bytes:
    """Quickly load one file from APK."""
    def _read_cdh(fh: BinaryIO) -> Tuple[bytes, int]:
        hdr = fh.read(46)
        if hdr[:4] != b"\x50\x4b\x01\x02":
            raise ZipError("Expected central directory file header signature")
        n, m, k = struct.unpack("<HHH", hdr[28:34])
        hdr += fh.read(n + m + k)
        return hdr[46:46 + n], int.from_bytes(hdr[42:46], "little")

    def _read_data(fh: BinaryIO, offset: int) -> bytes:
        fh.seek(offset)
        hdr = fh.read(30)
        if hdr[:4] != b"\x50\x4b\x03\x04":
            raise ZipError("Expected local file header signature")
        n, m = struct.unpack("<HH", hdr[26:30])
        hdr += fh.read(n + m)
        ctype = int.from_bytes(hdr[8:10], "little")
        csize = int.from_bytes(hdr[18:22], "little")
        if ctype == 0:
            return fh.read(csize)
        elif ctype == 8:
            return zlib.decompress(fh.read(csize), -15)
        else:
            raise ZipError(f"Unsupported compress_type {ctype}")

    filename_b = filename.encode()
    zdata = zip_data(apk)
    with open(apk, "rb") as fh:
        fh.seek(zdata.cd_offset)
        while fh.tell() < zdata.eocd_offset:
            name, offset = _read_cdh(fh)
            if name == filename_b:
                return _read_data(fh, offset)
    raise Error(f"Entry not found: {filename!r}")


def fnmatches_with_negation(filename: str, *patterns: str) -> bool:
    r"""
    Filename matching with shell patterns and negation.

    Checks whether filename matches any of the fnmatch patterns.

    An optional prefix "!" negates the pattern, invalidating a successful match
    by any preceding pattern; use a backslash ("\") in front of the first "!"
    for patterns that begin with a literal "!".

    >>> fnmatches_with_negation("foo.xml", "*", "!*.png")
    True
    >>> fnmatches_with_negation("foo.png", "*", "!*.png")
    False
    >>> fnmatches_with_negation("!foo.png", r"\!*.png")
    True

    """
    matches = False
    for p in patterns:
        if p.startswith("!"):
            if fnmatch(filename, p[1:]):
                matches = False
        else:
            if p.startswith(r"\!"):
                p = p[1:]
            if fnmatch(filename, p):
                matches = True
    return matches


def zip_data(apkfile: str, count: int = 1024) -> ZipData:
    """Extract central directory, EOCD, and offsets from ZIP."""
    with open(apkfile, "rb") as fh:
        fh.seek(-min(os.path.getsize(apkfile), count), os.SEEK_END)
        data = fh.read()
        pos = data.rfind(b"\x50\x4b\x05\x06")
        if pos == -1:
            raise ZipError("Expected end of central directory record (EOCD)")
        fh.seek(pos - len(data), os.SEEK_CUR)
        eocd_offset = fh.tell()
        fh.seek(16, os.SEEK_CUR)
        cd_offset = int.from_bytes(fh.read(4), "little")
        fh.seek(cd_offset)
        cd_and_eocd = fh.read()
    return ZipData(cd_offset, eocd_offset, cd_and_eocd)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="binres.py")
    subs = parser.add_subparsers(title="subcommands", dest="command")
    subs.required = True
    sub_dump = subs.add_parser("dump", help="parse & dump ARSC or AXML")
    sub_dump.add_argument("--apk", help="APK that contains the AXML/ARSC file(s)")
    sub_dump.add_argument("--json", action="store_true", help="output JSON")
    sub_dump.add_argument("--xml", action="store_true", help="output XML (AXML only)")
    sub_dump.add_argument("-v", "--verbose", action="store_true")
    sub_dump.add_argument("files_or_patterns", metavar="FILE_OR_PATTERN", nargs="+")
    sub_fastid = subs.add_parser("fastid", help="quickly get appid & version code/name")
    sub_fastid.add_argument("--json", action="store_true", help="output JSON")
    sub_fastid.add_argument("apks", metavar="APK", nargs="+")
    args = parser.parse_args()
    try:
        if args.command == "dump":
            if args.json and args.xml:
                raise Error("Conflicting options: --json and --xml")
            if args.apk:
                dump_apk(args.apk, *args.files_or_patterns, json=args.json,
                         verbose=args.verbose, xml=args.xml)
            else:
                dump(*args.files_or_patterns, json=args.json,
                     verbose=args.verbose, xml=args.xml)
        elif args.command == "fastid":
            fastid(*args.apks, json=args.json)
        else:
            raise Error(f"Unknown command: {args.command}")
    except Error as e:
        print(f"Error: {e}.", file=sys.stderr)
        sys.exit(1)
    except BrokenPipeError:
        pass

# vim: set tw=80 sw=4 sts=4 et fdm=marker :
