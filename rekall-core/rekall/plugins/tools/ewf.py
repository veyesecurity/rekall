# Rekall Memory Forensics
#
# Copyright 2014 Google Inc. All Rights Reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

"""This file provides read/write support for EWF files.

EWF files are generated by Encase/FTK and are a common compressible storage
format for digital evidence.

The below code is based on libewf:
https://github.com/libyal/libewf
https://googledrive.com/host/0B3fBvzttpiiSMTdoaVExWWNsRjg/


NOTE: Since EWFv1 files are unable to represent sparse data they are not
directly suitable for storing memory images. Therefore in Rekall we generally
use EWF files as containers for other formats, such as ELF core dumps.

NOTE: EWF files produced by the ewfacquire plugin are _NOT_ compatible with
Encase/FTK and can not be analyzed by those programs. We merely use the EWF
container as a container providing seekable compression for more traditional
memory image formats such as ELF.

When using the ewfacquire plugin, if the source address space contains a single
run of data, we generate a single EWF file of this run (e.g. for a disk
image). If, however, the source address space contains more than one run, we
automatically create an ELF core dump to contain the sparse runs, and that is
compressed into the EWF file instead. This is not generally compatible with
Encase or FTK since they do not understand layered address spaces! For Rekall
this works because Rekall automatically detects that the EWF file contains an
ELF core dump and stacks the relevant address spaces.
"""

__author__ = "Michael Cohen <scudette@google.com>"
import array
import os
import struct
import zlib

from rekall import obj
from rekall import plugin
from rekall import testlib
from rekall import utils
from rekall.plugins.addrspaces import elfcore
from rekall.plugins.addrspaces import standard
from rekall.plugins.overlays import basic


EWF_TYPES = dict(
    ewf_file_header_v1=[0x0d, {
        'EVF_sig': [0, ['Signature', dict(value="EVF\x09\x0d\x0a\xff\x00")]],

        'fields_start': [8, ['byte']],
        'segment_number': [9, ['unsigned short int']],
        'fields_end': [11, ['unsigned short int']],
        }],

    ewf_file_header_v2=[None, {
        'EVF_sig': [0, ['Signature', dict(value="EVF2\x0d\x0a\x81\x00")]],

        'major_version': [9, ['byte']],
        'minor_version': [10, ['byte']],

        'compression_method': [11, ['Enumeration', dict(
            target="unsigned short int",
            choices=dict(
                NONE=0,
                DEFLATE=1,
                BZIP2=2,
                )
            )]],
        'segment_number': [13, ['unsigned short int']],
        'set_identifier': [15, ['String', dict(length=16)]],
        }],

    ewf_section_descriptor_v1=[76, {
        # This string determines how to process this section.
        'type': [0, ['String', dict(length=16)]],

        # The next section in this file.
        'next': [16, ['Pointer', dict(
            target="ewf_section_descriptor_v1"
            )]],

        'size': [24, ['long long unsigned int']],
        'checksum': [72, ['int']],
        }],

    ewf_volume=[94, {
        'media_type': [0, ['Enumeration', dict(
            choices={
                0: 'remobable_disk',
                1: 'fixed_disk',
                2: 'optical_disk',
                3: 'LVF',
                4: 'memory',
                },
            )]],
        'number_of_chunks': [4, ['unsigned int']],
        'sectors_per_chunk': [8, ['unsigned int']],
        'bytes_per_sector': [12, ['unsigned int']],
        'number_of_sectors': [16, ['long long unsigned int']],
        'chs_cylinders': [24, ['unsigned int']],
        'chs_heads': [28, ['unsigned int']],
        'chs_sectors': [32, ['unsigned int']],

        'media_flags': [36, ['Flags', dict(
            maskmap={
                'image': 1,
                'physical': 2,
                'Fastblock Tableau write blocker': 4,
                'Tableau write blocker': 8
                })]],

        'compression_level': [52, ['Enumeration', dict(
            choices={
                0: 'no compression',
                1: 'fast/good compression',
                2: 'best compression',
                })]],

        'checksum': [90, ['int']],
        }],

    ewf_table_entry=[4, {
        # Is the chunk compressed?
        'compressed': [0, ['BitField', dict(start_bit=31, end_bit=32)]],

        # The offset to the chunk within the file.
        'offset': [0, ['BitField', dict(start_bit=0, end_bit=31)]],
        }],

    ewf_table_header_v1=[lambda x: x.entries[x.number_of_entries].obj_end, {
        'number_of_entries': [0, ['long long unsigned int']],
        'base_offset': [8, ['long long unsigned int']],
        'checksum': [20, ['int']],

        # The table just contains a list of table entries to the start of each
        # chunk.
        'entries': [24, ['Array', dict(
            target='ewf_table_entry',
            count=lambda x: x.number_of_entries
            )]],
        }],

    )


class ewf_section_descriptor_v1(obj.Struct):
    def UpdateChecksum(self):
        """Recalculate the checksum field."""
        self.size = self.next.v() - self.obj_offset
        data = self.obj_vm.read(
            self.obj_offset, self.checksum.obj_offset - self.obj_offset)

        self.checksum = zlib.adler32(data)


class ewf_table_header_v1(obj.Struct):
    def UpdateChecksum(self):
        """Recalculate the checksum field."""
        data = self.obj_vm.read(
            self.obj_offset, self.checksum.obj_offset - self.obj_offset)

        self.checksum = zlib.adler32(data)


class ewf_volume(ewf_table_header_v1):
    pass


class EWFProfile(basic.ProfileLLP64, basic.BasicClasses):
    """Basic profile for EWF files."""

    @classmethod
    def Initialize(cls, profile):
        super(EWFProfile, cls).Initialize(profile)

        profile.add_types(EWF_TYPES)
        profile.add_classes(
            ewf_section_descriptor_v1=ewf_section_descriptor_v1,
            ewf_table_header_v1=ewf_table_header_v1,
            ewf_volume=ewf_volume,
            )


class EWFFile(object):
    """A helper for parsing an EWF file."""

    def __init__(self, session=None, address_space=None):
        self.session = session

        # This is a cache of tables. We can quickly find the table responsible
        # for a particular chunk.
        self.tables = utils.SortedCollection(key=lambda x: x[0])
        self._chunk_offset = 0
        self.chunk_size = 32 * 1024

        # 32kb * 100 = 3.2mb cache size.
        self.chunk_cache = utils.FastStore(max_size=100)

        self.address_space = address_space
        self.profile = EWFProfile(session=session)
        self.file_header = self.profile.ewf_file_header_v1(
            offset=0, vm=self.address_space)

        # Make sure the file signature is correct.
        if not self.file_header.EVF_sig.is_valid():
            raise RuntimeError("EVF signature does not match.")

        # Now locate all the sections in the file.
        first_section = self.profile.ewf_section_descriptor_v1(
            vm=self.address_space, offset=self.file_header.obj_end)

        for section in first_section.walk_list("next"):
            if section.type == "header2":
                self.handle_header2(section)

            elif section.type == "header":
                self.handle_header(section)

            elif section.type in ["disk", "volume"]:
                self.handle_volume(section)

            elif section.type == "table":
                self.handle_table(section)

        # How many chunks we actually have in this file.
        self.size = self._chunk_offset * self.chunk_size

    def handle_header(self, section):
        """Handle the header section.

        We do not currently do anything with it.
        """
        # The old header contains an ascii encoded description, compressed with
        # zlib.
        data = zlib.decompress(
            section.obj_vm.read(section.obj_end, section.size))

        # We dont do anything with this data right now.

    def handle_header2(self, section):
        """Handle the header2 section.

        We do not currently do anything with it.
        """
        # The header contains a utf16 encoded description, compressed with zlib.
        data = zlib.decompress(
            section.obj_vm.read(section.obj_end, section.size)).decode("utf16")

        # We dont do anything with this data right now.

    def handle_volume(self, section):
        """Handle the volume section.

        We mainly use it to know the chunk size.
        """
        volume_header = self.profile.ewf_volume(
            vm=self.address_space, offset=section.obj_end)

        self.chunk_size = (volume_header.sectors_per_chunk *
                           volume_header.bytes_per_sector)

    def handle_table(self, section):
        """Parse the table and store it in our lookup table."""
        table_header = self.profile.ewf_table_header_v1(
            vm=self.address_space, offset=section.obj_end)

        number_of_entries = table_header.number_of_entries

        # This is an optimization which allows us to avoid small reads for each
        # chunk. We just load the entire table into memory and read it on demand
        # from there.
        table = array.array("I")
        table.fromstring(self.address_space.read(
            table_header.entries.obj_offset,
            4 * table_header.number_of_entries))

        # We assume the last chunk is a full chunk. Feeding zlib.decompress()
        # extra data does not matter so we just read the most we can.
        table.append(table[-1] + self.chunk_size)

        self.tables.insert(
            # First chunk for this table, table header, table entry cache.
            (self._chunk_offset, table_header, table))

        # The next table starts at this chunk.
        self._chunk_offset += number_of_entries

    def read_chunk(self, chunk_id):
        """Read a single chunk from the file."""
        try:
            return self.chunk_cache.Get(chunk_id)
        except KeyError:
            start_chunk, table_header, table = self.tables.find_le(chunk_id)

            # This should be a ewf_table_entry object but the below is faster.
            try:
                table_entry = table[chunk_id - start_chunk]

                offset = table_entry & 0x7fffffff
                next_offset = table[chunk_id - start_chunk + 1] & 0x7fffffff
                compressed_chunk_size = next_offset - offset
            except IndexError:
                return ""

            data = self.address_space.read(
                offset + table_header.base_offset, compressed_chunk_size)

            if table_entry & 0x80000000:
                data = zlib.decompress(data)

            # Cache the chunk for later.
            self.chunk_cache.Put(chunk_id, data)

            return data

    def read_partial(self, offset, length):
        """Read as much as possible from the current offset."""
        # Find the table responsible for this chunk.
        chunk_id, chunk_offset = divmod(offset, self.chunk_size)
        available_length = min(length, self.chunk_size - chunk_offset)

        # Get the chunk and split it.
        data = self.read_chunk(chunk_id)

        return data[chunk_offset:chunk_offset + available_length]

    def read(self, offset, length):
        """Read data from the file."""
        # Most read operations are very short and will not need to merge chunks
        # at all. In that case concatenating strings is much faster than storing
        # partial reads into a list and join()ing them.
        result = ''
        available_length = length

        while available_length > 0:
            buf = self.read_partial(offset, available_length)
            if not buf:
                break

            result += buf
            offset += len(buf)
            available_length -= len(buf)

        return result


class EWFFileWriter(object):
    """A writer for EWF files.

    NOTE: The EWF files we produce here are not generally compatible with
    Encase/FTK. We produce EWFv1 files which are unable to store sparse
    images. We place an ELF file inside the EWF container to ensure we can
    efficiently store sparse memory ranges.
    """

    def __init__(self, out_as, session):
        self.out_as = out_as
        self.session = session
        self.profile = EWFProfile(session=self.session)
        self.chunk_size = 32 * 1024
        self.current_offset = 0
        self.chunk_id = 0

        self.last_section = None

        # Start off by writing the file header.
        file_header = self.profile.ewf_file_header_v1(
            offset=0, vm=out_as)
        file_header.EVF_sig = file_header.EVF_sig.signature
        file_header.fields_start = 1
        file_header.segment_number = 1
        file_header.fields_end = 1

        self.current_offset = file_header.obj_end
        self.buffer = ""
        self.table_count = 0

        # Get ready to accept data.
        self.StartNewTable()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, trace):
        self.Close()

    def AddNewSection(self, section):
        if self.last_section:
            self.last_section.next = section
            self.last_section.UpdateChecksum()

        self.last_section = section

    def StartNewTable(self):
        """Writes a sections table and begins collecting chunks into table."""
        self.table = []
        self.table_count += 1

        sectors_section = self.profile.ewf_section_descriptor_v1(
            offset=self.current_offset, vm=self.out_as)
        sectors_section.type = "sectors"
        self.AddNewSection(sectors_section)

        self.base_offset = self.current_offset = sectors_section.obj_end

    def write(self, data):
        """Writes the data into the file.

        This method allows the writer to be used as a file-like object.
        """
        self.buffer += data
        buffer_offset = 0
        while len(self.buffer) - buffer_offset >= self.chunk_size:
            data = self.buffer[buffer_offset:buffer_offset+self.chunk_size]
            cdata = zlib.compress(data)
            chunk_offset = self.current_offset - self.base_offset

            if len(cdata) > len(data):
                self.table.append(chunk_offset)
                cdata = data
            else:
                self.table.append(0x80000000 | chunk_offset)

            self.out_as.write(self.current_offset, cdata)
            self.current_offset += len(cdata)
            buffer_offset += self.chunk_size
            self.chunk_id += 1

            # Flush the table when it gets too large. Tables can only store 31
            # bit offset and so can only address roughly 2gb. We choose to stay
            # under 1gb: 30000 * 32kb = 0.91gb.
            if len(self.table) > 30000:
                self.session.report_progress(
                    "Flushing EWF Table %s.", self.table_count)
                self.FlushTable()
                self.StartNewTable()

        self.buffer = self.buffer[buffer_offset:]

    def FlushTable(self):
        """Flush the current table."""
        table_section = self.profile.ewf_section_descriptor_v1(
            offset=self.current_offset, vm=self.out_as)
        table_section.type = "table"
        self.AddNewSection(table_section)

        table_header = self.profile.ewf_table_header_v1(
            offset=table_section.obj_end, vm=self.out_as)

        table_header.number_of_entries = len(self.table)
        table_header.base_offset = self.base_offset
        table_header.UpdateChecksum()

        # Now write the table section.
        self.out_as.write(
            table_header.entries.obj_offset,
            struct.pack("I" * len(self.table), *self.table))

        self.current_offset = (table_header.entries.obj_offset +
                               4 * len(self.table))
    def Close(self):
        # If there is some data left over, pad it to the length of the chunk so
        # we get to write it.
        if len(self.buffer):
            self.write("\x00" * (self.chunk_size - len(self.buffer)))

        self.FlushTable()

        # Write the volume section.
        volume_section = self.profile.ewf_section_descriptor_v1(
            offset=self.current_offset, vm=self.out_as)
        volume_section.type = "volume"
        self.AddNewSection(volume_section)

        volume_header = self.profile.ewf_volume(
            offset=volume_section.obj_end, vm=self.out_as)

        volume_header.number_of_chunks = self.chunk_id
        volume_header.sectors_per_chunk = self.chunk_size / 512
        volume_header.number_of_sectors = (volume_header.number_of_chunks *
                                           volume_header.sectors_per_chunk)

        volume_header.bytes_per_sector = 512
        volume_header.UpdateChecksum()

        # Write the done section.
        done_section = self.profile.ewf_section_descriptor_v1(
            offset=volume_header.obj_end, vm=self.out_as)
        done_section.type = "done"
        self.AddNewSection(done_section)

        # Last section points to itself.
        self.AddNewSection(done_section)


class EWFAcquire(plugin.PhysicalASMixin, plugin.Command):
    """Copy the physical address space to an EWF file."""

    name = "ewfacquire"

    @classmethod
    def args(cls, parser):
        super(EWFAcquire, cls).args(parser)

        parser.add_argument(
            "destination", default=None, required=False,
            help="The destination file to create. "
            "If not specified we write output.E01 in current directory.")

    def __init__(self, destination=None, **kwargs):
        super(EWFAcquire, self).__init__(**kwargs)

        self.destination = destination

    def render(self, renderer):
        if self.destination is None:
            out_fd = renderer.open(filename="output.E01", mode="w+b")
        else:
            directory, filename = os.path.split(self.destination)
            out_fd = renderer.open(filename=filename, directory=directory,
                                   mode="w+b")

        with out_fd:
            runs = list(self.physical_address_space.get_mappings())

            out_address_space = standard.WritableFDAddressSpace(
                fhandle=out_fd, session=self.session)

            with EWFFileWriter(
                out_address_space, session=self.session) as writer:
                if len(runs) > 1:
                    elfcore.WriteElfFile(
                        self.physical_address_space,
                        writer, session=self.session)

                else:
                    last_address = runs[0].end
                    block_size = 1024 * 1024

                    for offset in xrange(0, last_address, block_size):
                        available_length = min(block_size, last_address-offset)
                        data = self.physical_address_space.read(
                            offset, available_length)

                        self.session.report_progress(
                            "Writing %sMB", offset/1024/1024)

                        writer.write(data)


class TestEWFAcquire(testlib.HashChecker):
    PARAMETERS = dict(commandline="ewfacquire %(tempdir)s/output_image.e01")
