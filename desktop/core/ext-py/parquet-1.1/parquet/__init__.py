from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import gzip
import io
import json
import logging
import os
import struct
import sys

from collections import defaultdict
try:
  from collections import OrderedDict
except ImportError:
  from ordereddict import OrderedDict # Python 2.6

import thriftpy
from thriftpy.protocol.compact import TCompactProtocolFactory

from .converted_types import convert_column
from .thrift_filetransport import TFileTransport
from . import encoding
from . import schema

PY3 = sys.version_info > (3,)

if PY3:
    import csv
else:
    from backports import csv

THRIFT_FILE = os.path.join(os.path.dirname(__file__), "parquet.thrift")
parquet_thrift = thriftpy.load(THRIFT_FILE, module_name="parquet_thrift")



logger = logging.getLogger("parquet")

try:
    import snappy
except ImportError:
    logger.warn(
        "Couldn't import snappy. Support for snappy compression disabled.")


class ParquetFormatException(Exception):
    pass


def _check_header_magic_bytes(fo):
    "Returns true if the file-like obj has the PAR1 magic bytes at the header"
    fo.seek(0, 0)
    magic = fo.read(4)
    return magic == b'PAR1'


def _check_footer_magic_bytes(fo):
    "Returns true if the file-like obj has the PAR1 magic bytes at the footer"
    fo.seek(-4, 2)  # seek to four bytes from the end of the file
    magic = fo.read(4)
    return magic == b'PAR1'


def _get_footer_size(fo):
    "Readers the footer size in bytes, which is serialized as little endian"
    fo.seek(-8, 2)
    tup = struct.unpack(b"<i", fo.read(4))
    return tup[0]


def _read_footer(fo):
    """Reads the footer from the given file object, returning a FileMetaData
    object. This method assumes that the fo references a valid parquet file"""
    footer_size = _get_footer_size(fo)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Footer size in bytes: %s", footer_size)
    fo.seek(-(8 + footer_size), 2)  # seek to beginning of footer
    tin = TFileTransport(fo)
    pin = TCompactProtocolFactory().get_protocol(tin)
    fmd = parquet_thrift.FileMetaData()
    fmd.read(pin)
    return fmd


def _read_page_header(fo):
    """Reads the page_header from the given fo"""
    tin = TFileTransport(fo)
    pin = TCompactProtocolFactory().get_protocol(tin)
    ph = parquet_thrift.PageHeader()
    ph.read(pin)
    return ph


def read_footer(filename):
    """Reads and returns the FileMetaData object for the given file."""
    with open(filename, 'rb') as fo:
        if not _check_header_magic_bytes(fo) or \
           not _check_footer_magic_bytes(fo):
            raise ParquetFormatException("{0} is not a valid parquet file "
                                         "(missing magic bytes)"
                                         .format(filename))
        return _read_footer(fo)


def _get_name(type_, value):
    """Returns the name for the given value of the given type_ unless value is
    None, in which case it returns empty string"""
    return type_._VALUES_TO_NAMES[value] if value is not None else "None"


def _get_offset(cmd):
    """Returns the offset into the cmd based upon if it's a dictionary page or
    a data page"""
    dict_offset = cmd.dictionary_page_offset
    data_offset = cmd.data_page_offset
    if dict_offset is None or data_offset < dict_offset:
        return data_offset
    return dict_offset


def dump_metadata(filename, show_row_group_metadata, out=sys.stdout):
    def println(value):
        out.write(value + "\n")
    footer = read_footer(filename)
    println("File Metadata: {0}".format(filename))
    println("  Version: {0}".format(footer.version))
    println("  Num Rows: {0}".format(footer.num_rows))
    println("  k/v metadata: ")
    if footer.key_value_metadata and len(footer.key_value_metadata) > 0:
        for kv in footer.key_value_metadata:
            println("    {0}={1}".format(kv.key, kv.value))
    else:
        println("    (none)")
    println("  schema: ")
    for se in footer.schema:
        println("    {name} ({type}): length={type_length}, "
                "repetition={repetition_type}, "
                "children={num_children}, "
                "converted_type={converted_type}".format(
                    name=se.name,
                    type=parquet_thrift.Type._VALUES_TO_NAMES[se.type] if se.type else None,
                    type_length=se.type_length,
                    repetition_type=_get_name(parquet_thrift.FieldRepetitionType,
                                              se.repetition_type),
                    num_children=se.num_children,
                    converted_type=se.converted_type))
    if show_row_group_metadata:
        println("  row groups: ")
        for rg in footer.row_groups:
            num_rows = rg.num_rows
            bytes = rg.total_byte_size
            println(
                "  rows={num_rows}, bytes={bytes}".format(num_rows=num_rows,
                                                          bytes=bytes))
            println("    chunks:")
            for cg in rg.columns:
                cmd = cg.meta_data
                println("      type={type} file_offset={offset} "
                        "compression={codec} "
                        "encodings={encodings} path_in_schema={path_in_schema} "
                        "num_values={num_values} uncompressed_bytes={raw_bytes} "
                        "compressed_bytes={compressed_bytes} "
                        "data_page_offset={data_page_offset} "
                        "dictionary_page_offset={dictionary_page_offset}".format(
                            type=_get_name(parquet_thrift.Type, cmd.type),
                            offset=cg.file_offset,
                            codec=_get_name(parquet_thrift.CompressionCodec, cmd.codec),
                            encodings=",".join(
                                [_get_name(
                                    parquet_thrift.Encoding, s) for s in cmd.encodings]),
                            path_in_schema=cmd.path_in_schema,
                            num_values=cmd.num_values,
                            raw_bytes=cmd.total_uncompressed_size,
                            compressed_bytes=cmd.total_compressed_size,
                            data_page_offset=cmd.data_page_offset,
                            dictionary_page_offset=cmd.dictionary_page_offset))
                with open(filename, 'rb') as fo:
                    offset = _get_offset(cmd)
                    fo.seek(offset, 0)
                    values_read = 0
                    println("      pages: ")
                    while values_read < num_rows:
                        ph = _read_page_header(fo)
                        # seek past current page.
                        fo.seek(ph.compressed_page_size, 1)
                        daph = ph.data_page_header
                        type_ = _get_name(parquet_thrift.PageType, ph.type)
                        raw_bytes = ph.uncompressed_page_size
                        num_values = None
                        if ph.type == parquet_thrift.PageType.DATA_PAGE:
                            num_values = daph.num_values
                            values_read += num_values
                        if ph.type == parquet_thrift.PageType.DICTIONARY_PAGE:
                            pass
                            #num_values = diph.num_values

                        encoding_type = None
                        def_level_encoding = None
                        rep_level_encoding = None
                        if daph:
                            encoding_type = _get_name(parquet_thrift.Encoding, daph.encoding)
                            def_level_encoding = _get_name(
                                parquet_thrift.Encoding, daph.definition_level_encoding)
                            rep_level_encoding = _get_name(
                                parquet_thrift.Encoding, daph.repetition_level_encoding)

                        println("        page header: type={type} "
                                "uncompressed_size={raw_bytes} "
                                "num_values={num_values} encoding={encoding} "
                                "def_level_encoding={def_level_encoding} "
                                "rep_level_encoding={rep_level_encoding}".format(
                                    type=type_,
                                    raw_bytes=raw_bytes,
                                    num_values=num_values,
                                    encoding=encoding_type,
                                    def_level_encoding=def_level_encoding,
                                    rep_level_encoding=rep_level_encoding))


def _read_page(fo, page_header, column_metadata):
    """Internal function to read the data page from the given file-object
    and convert it to raw, uncompressed bytes (if necessary)."""
    bytes_from_file = fo.read(page_header.compressed_page_size)
    codec = column_metadata.codec
    if codec is not None and codec != parquet_thrift.CompressionCodec.UNCOMPRESSED:
        if column_metadata.codec == parquet_thrift.CompressionCodec.SNAPPY:
            raw_bytes = snappy.decompress(bytes_from_file)
        elif column_metadata.codec == parquet_thrift.CompressionCodec.GZIP:
            io_obj = io.BytesIO(bytes_from_file)
            with gzip.GzipFile(fileobj=io_obj, mode='rb') as f:
                raw_bytes = f.read()
        else:
            raise ParquetFormatException(
                "Unsupported Codec: {0}".format(codec))
    else:
        raw_bytes = bytes_from_file

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Read page with compression type {0}. Bytes {1} -> {2}".format(
            _get_name(parquet_thrift.CompressionCodec, codec),
            page_header.compressed_page_size,
            page_header.uncompressed_page_size))
    assert len(raw_bytes) == page_header.uncompressed_page_size, \
        "found {0} raw bytes (expected {1})".format(
            len(raw_bytes),
            page_header.uncompressed_page_size)
    return raw_bytes


def _read_data(fo, fo_encoding, value_count, bit_width):
    """Internal method to read data from the file-object using the given
    encoding. The data could be definition levels, repetition levels, or
    actual values.
    """
    vals = []
    if fo_encoding == parquet_thrift.Encoding.RLE:
        seen = 0
        while seen < value_count:
            values = encoding.read_rle_bit_packed_hybrid(fo, bit_width)
            if values is None:
                break  # EOF was reached.
            vals += values
            seen += len(values)
    elif fo_encoding == parquet_thrift.Encoding.BIT_PACKED:
        raise NotImplementedError("Bit packing not yet supported")

    return vals


def read_data_page(fo, schema_helper, page_header, column_metadata,
                   dictionary):
    """Reads the datapage from the given file-like object based upon the
    metadata in the schema_helper, page_header, column_metadata, and
    (optional) dictionary. Returns a list of values.
    """
    daph = page_header.data_page_header
    raw_bytes = _read_page(fo, page_header, column_metadata)
    io_obj = io.BytesIO(raw_bytes)
    vals = []
    debug_logging = logger.isEnabledFor(logging.DEBUG)

    if debug_logging:
        logger.debug("  definition_level_encoding: %s",
                     _get_name(parquet_thrift.Encoding, daph.definition_level_encoding))
        logger.debug("  repetition_level_encoding: %s",
                     _get_name(parquet_thrift.Encoding, daph.repetition_level_encoding))
        logger.debug("  encoding: %s", _get_name(parquet_thrift.Encoding, daph.encoding))

    # definition levels are skipped if data is required.
    definition_levels = None
    num_nulls = 0
    max_definition_level = -1
    if not schema_helper.is_required(column_metadata.path_in_schema[-1]):
        max_definition_level = schema_helper.max_definition_level(
            column_metadata.path_in_schema)
        bit_width = encoding.width_from_max_int(max_definition_level)
        if debug_logging:
            logger.debug("  max def level: %s   bit_width: %s",
                         max_definition_level, bit_width)
        if bit_width == 0:
            definition_levels = [0] * daph.num_values
        else:
            definition_levels = _read_data(io_obj,
                                           daph.definition_level_encoding,
                                           daph.num_values,
                                           bit_width)[:daph.num_values]

        # any thing that isn't at max definition level is a null.
        num_nulls = len(definition_levels) - definition_levels.count(max_definition_level)
        if debug_logging:
            logger.debug("  Definition levels: %s", len(definition_levels))

    # repetition levels are skipped if data is at the first level.
    repetition_levels = None
    if len(column_metadata.path_in_schema) > 1:
        max_repetition_level = schema_helper.max_repetition_level(
            column_metadata.path_in_schema)
        bit_width = encoding.width_from_max_int(max_repetition_level)
        repetition_levels = _read_data(io_obj,
                                       daph.repetition_level_encoding,
                                       daph.num_values,
                                       bit_width)

    # TODO Actually use the repetition levels.
    if daph.encoding == parquet_thrift.Encoding.PLAIN:
        read_values = \
            encoding.read_plain(io_obj, column_metadata.type, daph.num_values - num_nulls)
        if definition_levels:
            it = iter(read_values)
            vals.extend([next(it) if level == max_definition_level else None for level in definition_levels])
        else:
            vals.extend(read_values)
        if debug_logging:
            logger.debug("  Values: %s, nulls: %s", len(vals), num_nulls)
    elif daph.encoding == parquet_thrift.Encoding.PLAIN_DICTIONARY:
        # bit_width is stored as single byte.
        bit_width = struct.unpack("<B", io_obj.read(1))[0]
        if debug_logging:
            logger.debug("bit_width: %d", bit_width)
        total_seen = 0
        dict_values_bytes = io_obj.read()
        dict_values_io_obj = io.BytesIO(dict_values_bytes)
        # TODO jcrobak -- not sure that this loop is needed?
        while total_seen < daph.num_values:
            values = encoding.read_rle_bit_packed_hybrid(
                dict_values_io_obj, bit_width, len(dict_values_bytes))
            if len(values) + total_seen > daph.num_values:
                values = values[0: daph.num_values - total_seen]
            vals += [dictionary[v] for v in values]
            total_seen += len(values)
    else:
        raise ParquetFormatException("Unsupported encoding: %s",
                                     _get_name(Encoding, daph.encoding))
    return vals


def read_dictionary_page(fo, page_header, column_metadata):
    raw_bytes = _read_page(fo, page_header, column_metadata)
    io_obj = io.BytesIO(raw_bytes)
    return encoding.read_plain(io_obj, column_metadata.type,
        page_header.dictionary_page_header.num_values)


def DictReader(fo, columns=None):
    """
    Reader for a parquet file object.

    This function is a generator returning an OrderedDict for each row
    of data in the parquet file. Nested values will be flattend into the
    top-level dict and can be referenced with '.' notation (e.g. 'foo' -> 'bar'
    is referenced as 'foo.bar')

    :param fo: the file containing parquet data
    :param columns: the columns to include. If None (default), all columns
                    are included. Nested values are referenced with "." notation
    """
    footer = _read_footer(fo)
    keys = columns if columns else [s.name for s in
                                    footer.schema if s.type]

    for row in reader(fo, columns):
        yield OrderedDict(zip(keys, row))

def reader(fo, columns=None):
    """
    Reader for a parquet file object.

    This function is a generator returning a list of values for each row
    of data in the parquet file.

    :param fo: the file containing parquet data
    :param columns: the columns to include. If None (default), all columns
                    are included. Nested values are referenced with "." notation
    """
    if hasattr(fo, 'mode') and 'b' not in fo.mode:
        logger.error("parquet.reader requires the fileobj to be opened in binary mode!")
    footer = _read_footer(fo)
    schema_helper = schema.SchemaHelper(footer.schema)
    keys = columns if columns else [s.name for s in
                                    footer.schema if s.type]
    debug_logging = logger.isEnabledFor(logging.DEBUG)
    for rg in footer.row_groups:
        res = defaultdict(list)
        row_group_rows = rg.num_rows
        for idx, cg in enumerate(rg.columns):
            dict_items = []
            cmd = cg.meta_data
            # skip if the list of columns is specified and this isn't in it
            if columns and not ".".join(cmd.path_in_schema) in columns:
                continue

            offset = _get_offset(cmd)
            fo.seek(offset, 0)
            values_seen = 0
            if debug_logging:
                logger.debug("reading column chunk of type: %s",
                             _get_name(parquet_thrift.Type, cmd.type))
            while values_seen < row_group_rows:
                ph = _read_page_header(fo)
                if debug_logging:
                    logger.debug("Reading page (type=%s, "
                                 "uncompressed=%s bytes, "
                                 "compressed=%s bytes)",
                                 _get_name(parquet_thrift.PageType, ph.type),
                                 ph.uncompressed_page_size,
                                 ph.compressed_page_size)

                if ph.type == parquet_thrift.PageType.DATA_PAGE:
                    values = read_data_page(fo, schema_helper, ph, cmd,
                                            dict_items)
                    schema_element = schema_helper.schema_element(cmd.path_in_schema[-1])
                    res[".".join(cmd.path_in_schema)] += convert_column(values,
                                                                        schema_element) if schema_element.converted_type else values
                    values_seen += ph.data_page_header.num_values
                elif ph.type == parquet_thrift.PageType.DICTIONARY_PAGE:
                    if debug_logging:
                        logger.debug(ph)
                    assert dict_items == []
                    dict_items = read_dictionary_page(fo, ph, cmd)
                    if debug_logging:
                        logger.debug("Dictionary: %s", str(dict_items))
                else:
                    logger.warn("Skipping unknown page type={0}".format(
                        _get_name(parquet_thrift.PageType, ph.type)))

        for i in range(rg.num_rows):
            yield [res[k][i] for k in keys if res[k]]

class JsonWriter(object):
    def __init__(self, out):
        self._out = out

    def writerow(self, row):
        json_text = json.dumps(row)
        if type(json_text) is bytes:
            json_text = json_text.decode('utf-8')
        self._out.write(json_text)
        self._out.write(u'\n')

def _dump(fo, options, out=sys.stdout):

    # writer and keys are lazily loaded. We don't know the keys until we have
    # the first item. And we need the keys for the csv writer.
    total_count = 0
    writer = None
    keys = None
    for row in DictReader(fo, options.col):
        if not keys:
            keys = row.keys()
        if not writer:
            writer = csv.DictWriter(out, keys, delimiter=u'\t', quotechar=u'\'',
                quoting=csv.QUOTE_MINIMAL) if options.format == 'csv' \
                    else JsonWriter(out) if options.format == 'json' \
                    else None
        if total_count == 0 and options.format == "csv" and not options.no_headers:
            writer.writeheader()
        if options.limit != -1 and total_count >= options.limit:
            return
        row_unicode = dict((k, (v.decode("utf-8") if type(v) is bytes else v)) for k, v in row.items())
        writer.writerow(row_unicode)
        total_count += 1


def dump(filename, options, out=sys.stdout):
    with open(filename, 'rb') as fo:
        return _dump(fo, options=options, out=out)
