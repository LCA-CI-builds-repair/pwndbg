"""
Global Offset Table Tracker

Subsystem for tracking accesses to external function calls made through pointers
in an inferior's Global Offset Table, such as those made by the stubs in the
Procedure Linkage Table.

Currently, it does this by attatching watchpoints to the entries in the GOT and
taking note of where the call came from, but it could be done much faster by
injecting our own code into the program space to track this.
"""

from __future__ import annotations

import gdb

import pwndbg.color.message as message
import pwndbg.gdblib.arch
import pwndbg.gdblib.bpoint
import pwndbg.gdblib.dynamic
import pwndbg.gdblib.memory
import pwndbg.gdblib.shellcode
import pwndbg.gdblib.typeinfo
import pwndbg.gdblib.vmmap


class RelocTypes:
    """
    This class contains all the relocation type constants so that one may
    interpret the relocations types present in the DYNAMIC segment. These
    constants are defined in each of the processors' SystemV R4 psABI document,
    or equivalent, and should stay the same across all implementations of libc
    on systems that adhere to that ABI, such as Linux.

    Most of these were sourced from GLibc, which conveniently lists all of the
    relocations types in a single file[1].

    [1]: https://elixir.bootlin.com/glibc/glibc-2.38/source/elf/elf.h
    """

    R_RISCV_JUMP_SLOT = 5
    R_X86_64_JUMP_SLOT = 7
    R_386_JMP_SLOT = 7
    R_CRIS_JUMP_SLOT = 11
    R_390_JMP_SLOT = 11
    R_CKCORE_JUMP_SLOT = 12
    R_TILEPRO_JMP_SLOT = 12
    R_MICROBLAZE_JUMP_SLOT = 17
    R_TILEGX_JMP_SLOT = 18
    R_OR1K_JMP_SLOT = 20
    R_68K_JMP_SLOT = 21
    R_SPARC_JMP_SLOT = 21
    R_PPC_JMP_SLOT = 21
    R_PPC64_JMP_SLOT = 21
    R_ARM_JUMP_SLOT = 22
    R_MN10300_JMP_SLOT = 22
    R_ALPHA_JMP_SLOT = 26
    R_NIOS2_JUMP_SLOT = 38
    R_NDS32_JMP_SLOT = 41
    R_METAG_JMP_SLOT = 44
    R_M32R_JMP_SLOT = 52
    R_ARC_JMP_SLOT = 55
    R_MIPS_JUMP_SLOT = 127
    R_SH_JMP_SLOT = 164
    R_AARCH64_JUMP_SLOT = 1026

    R_X86_64_IRELATIVE = 37
    R_386_IRELATIVE = 42
    R_RISCV_IRELATIVE = 58
    R_390_IRELATIVE = 61
    R_ARM_IRELATIVE = 160
    R_AARCH64_P32_IRELATIVE = 188
    R_PPC_IRELATIVE = 248
    R_PPC64_IRELATIVE = 248
    R_SPARC_IRELATIVE = 249
    R_AARCH64_IRELATIVE = 1032


# Set of all type codes associated with jump slots, by architecture.
JUMP_SLOTS = {
    "x86-64": set([RelocTypes.R_X86_64_JUMP_SLOT]),
    "i386": set([RelocTypes.R_386_JMP_SLOT]),
    "aarch64": set([RelocTypes.R_AARCH64_JUMP_SLOT]),
    "mips": set([RelocTypes.R_MIPS_JUMP_SLOT]),
    "powerpc": set([RelocTypes.R_PPC_JMP_SLOT]),
    "sparc": set([RelocTypes.R_SPARC_JMP_SLOT]),
    "arm": set([RelocTypes.R_ARM_JUMP_SLOT]),
    "armcm": set([RelocTypes.R_ARM_JUMP_SLOT]),
    "rv32": set([RelocTypes.R_RISCV_JUMP_SLOT]),
    "rv64": set([RelocTypes.R_RISCV_JUMP_SLOT]),
}

# Set of all type codes associated with irelative jump slots, by architecture.
IRELATIVE_SLOTS = {
    "x86-64": set([RelocTypes.R_X86_64_IRELATIVE]),
    "i386": set([RelocTypes.R_386_IRELATIVE]),
    "aarch64": set([RelocTypes.R_AARCH64_P32_IRELATIVE, RelocTypes.R_AARCH64_IRELATIVE]),
    "mips": set([]),
    "powerpc": set([RelocTypes.R_PPC_IRELATIVE]),
    "sparc": set([RelocTypes.R_SPARC_IRELATIVE]),
    "arm": set([RelocTypes.R_ARM_IRELATIVE]),
    "armcm": set([RelocTypes.R_ARM_IRELATIVE]),
    "rv32": set([RelocTypes.R_RISCV_IRELATIVE]),
    "rv64": set([RelocTypes.R_RISCV_IRELATIVE]),
}


def is_mmap_error(ptr):
    """
    Checks whether the return value of an mmap of indicates an error.
    """
    err = ((1 << pwndbg.gdblib.arch.ptrsize) - 1) & pwndbg.lib.memory.PAGE_MASK
    return ptr & pwndbg.lib.memory.PAGE_MASK == err


class TrapAllocator:
    """
    Utility that allocates and manages executable addresses in the space of the
    executing program that we can trap.
    """

    block_capacity = 4096
    slot_size = 8

    blocks = []
    current_block_occupancy = 0
    vacant_slots = []

    occupied_slots = set()

    def alloc(self):
        """
        Allocates a new address to where program execution can be diverted.
        """
        if len(self.vacant_slots) > 0:
            # We have an easy vacant slot we can recycle.
            addr = self.vacant_slots.pop()
            self.occupied_slots.add(addr)
            return addr

        if len(self.blocks) > 0 and self.current_block_occupancy < self.block_capacity:
            # We have a non-full block we can allocate a new slot from.
            addr = self.blocks[-1] + self.current_block_occupancy * self.slot_size
            self.current_block_occupancy += 1
            self.occupied_slots.add(addr)
            return addr

        # We have to allocate a new block.
        block_base = pwndbg.gdblib.shellcode.exec_syscall(
            "SYS_mmap",
            0,
            self.block_capacity * self.slot_size,
            5,  # PROT_READ | PROT_EXEC
            0x22,  # MAP_PRIVATE | MAP_ANONYMOUS
            -1,
            0,
        )
        if is_mmap_error(block_base):
            raise RuntimeError(f"SYS_mmap request returned {block_base:#x}")

        self.blocks.append(block_base)
        self.current_block_occupancy = 1
        addr = self.blocks[-1]

        self.occupied_slots.add(addr)
        return addr

    def free(self, address):
        """
        Indicates that an address obtained from alloc() can be recycled.
        """
        assert address in self.occupied_slots
        self.occupied_slots.remove(address)
        self.vacant_slots.append(address)

    def clear(self):
        """
        Deletes all memory mappings and frees all addresses.
        """
        size = self.block_capacity * self.slot_size
class TrapAllocator:
    def allocate(self, size):
        pass

# Whether the GOT tracking is currently enabled.
GOT_TRACKING = False

# Map describing all of the currently installed analysis watchpoints.
INSTALLED_WATCHPOINTS = {}


class Patcher(pwndbg.gdblib.bpoint.BreakpointEvent):
    """
    Watches for changes made by program code to the GOT and fixes them up.
    """

    entry = 0
    tracker = None

    def __init__(self, entry, tracker):
        super().__init__(
            f"*(void**){entry:#x}", type=gdb.BP_WATCHPOINT, wp_class=gdb.WP_WRITE, internal=True
        )
        self.silent = True
        self.entry = entry
        self.tracker = tracker

    def on_breakpoint_hit(self):
        # Read the new branch target, and update the redirection target of the
        # tracker accordingly.
        new_target = pwndbg.gdblib.memory.pvoid(self.entry)
        if new_target == self.tracker.trapped_address:
            return

        self.tracker.target = new_target

        print(
            f"Attempted write at entry {self.entry:#x} for symbol {self.tracker.dynamic_section.string(self.tracker.dynamic_section.symtab_read(self.tracker.relocation_fn(self.tracker.relocation_index, 'r_sym'), 'st_name'))}. {self.tracker.trapped_address:#x} -> {new_target:#x}"
        )
        # Update the GOT entry so that it points to the trapped address again.
        #
        # FIXME: Ideally, we'd use gdb.Value([...]).assign() here, but that is
        # not always available, so we must do this ugly hack instead.
        gdb.execute(f"set *(void**){self.entry:#x} = {self.tracker.trapped_address:#x}")


class Tracker(pwndbg.gdblib.bpoint.BreakpointEvent):
    """
    Class that tracks the accesses made to the entries in the GOT.
    """

    hits = {}
    total_hits = 0
    trapped_address = 0

    target = 0
    dynamic_section = None
    relocation_fn = None
    relocation_index = 0
    link_map_entry = None

    def __init__(self):
        self.trapped_address = TRAP_ALLOCATOR.alloc()
        super().__init__(f"*{self.trapped_address:#x}", internal=True)
        self.silent = True

    def delete(self):
        TRAP_ALLOCATOR.free(self.trapped_address)
        super().delete()

    def on_breakpoint_hit(self):
        # Collect the stack that accessed this GOT entry.
        print(
            f"Hit trapped address {self.trapped_address:#x} of symbol {self.dynamic_section.string(self.dynamic_section.symtab_read(self.relocation_fn(self.relocation_index, 'r_sym'), 'st_name'))}, redirecting to {self.target:#x}"
        )
        stack = [pwndbg.gdblib.regs.pc]
        frame = gdb.newest_frame().older()
        while frame is not None:
            stack.append(frame.pc())
            frame = frame.older()
        stack = tuple(stack)
        if stack not in self.hits:
            self.hits[stack] = 0
        self.hits[stack] += 1
        self.total_hits += 1

        # Divert execution back to the real jump target.
        gdb.execute(f"set $pc = {self.target}")


def _update_watchpoints():
    """
    Internal function responsible for updating the watchpoints that track the
    accesses to the GOT.
    """
    if not GOT_TRACKING:
        # We don't want to bother anyone.
        return

    # Remove the watchpoints that are currently enabled.
    for _, (tracker, patcher) in INSTALLED_WATCHPOINTS.items():
        patcher.delete()
        tracker.delete()
    INSTALLED_WATCHPOINTS.clear()

    # Install new watchpoints to cover all of the jump slots in all GOTs
    for obj in pwndbg.gdblib.dynamic.link_map():
        name = obj.name()
        if name == b"":
            name = pwndbg.gdblib.proc.exe

        try:
            dynamic = pwndbg.gdblib.dynamic.DynamicSegment(obj.dynamic(), obj.load_bias())
        except RuntimeError as e:
            print(message.warn(f"object {name} has invalid DYNAMIC section: {e}"))
            continue

        jump_slots = JUMP_SLOTS[pwndbg.gdblib.arch.name]
        if dynamic.has_rel:
            for i in range(dynamic.rel_entry_count()):
                if dynamic.rel_read(i, "r_type") not in jump_slots:
                    continue
                target = dynamic.load_bias + dynamic.rel_read(i, "r_offset")

                tracker = Tracker()
                tracker.dynamic_section = dynamic
                tracker.link_map_entry = obj
                tracker.reloction_index = i
                tracker.relocation_fn = dynamic.rel_read
                patcher = Patcher(target, tracker)
                patcher.on_breakpoint_hit()

                INSTALLED_WATCHPOINTS[target] = (tracker, patcher)
        if dynamic.has_rela:
            for i in range(dynamic.rela_entry_count()):
                if dynamic.rela_read(i, "r_type") not in jump_slots:
                    continue
                target = (
                    dynamic.load_bias
                    + dynamic.rela_read(i, "r_offset")
                    + dynamic.rela_read(i, "r_addend")
                )

                tracker = Tracker()
                tracker.dynamic_section = dynamic
                tracker.link_map_entry = obj
                tracker.reloction_index = i
                tracker.relocation_fn = dynamic.rela_read
                patcher = Patcher(target, tracker)
                patcher.on_breakpoint_hit()

                INSTALLED_WATCHPOINTS[target] = (tracker, patcher)
        if dynamic.has_jmprel:
            for i in range(dynamic.jmprel_entry_count()):
                if dynamic.jmprel_read(i, "r_type") not in jump_slots:
                    continue
                target = dynamic.load_bias + dynamic.jmprel_read(i, "r_offset")
                if dynamic.jmprel_elem.has_field("r_addend"):
                    target += dynamic.jmprel_read(i, "r_addend")

                tracker = Tracker()
                tracker.dynamic_section = dynamic
                tracker.link_map_entry = obj
                tracker.reloction_index = i
                tracker.relocation_fn = dynamic.jmprel_read
                patcher = Patcher(target, tracker)
                patcher.on_breakpoint_hit()

                INSTALLED_WATCHPOINTS[target] = (tracker, patcher)


# Set the function so that it's called whenever the link map changes.
pwndbg.gdblib.dynamic.r_debug_link_map_changed_add_listener(_update_watchpoints)


def all_tracked_entries():
    """
    Return an iterator over all of the GOT whose accesses are being tracked.
    """
    return INSTALLED_WATCHPOINTS.items()


def writable_tracked_entries():
    """
    Return an iterator over all of the tracked GOT entries in writable sections.
    """
    for addr, item in all_tracked_entries():
        if pwndbg.gdblib.vmmap.find(addr).write:
            yield addr, item


def enable_got_call_tracking(disable_hardware_whatchpoints=True):
    """
    Enable the analysis of calls made through the GOT.
    """

    # Disable hardware watchpoints.
    #
    # We don't really know how to make sure that the hardware watchpoints
    # present in the system have enough capabilities for them to be useful to
    # us in this module, seeing as what they can do varies considerably between
    # systems and failures are fairly quiet and, thus, hard to detect[1].
    # Because of this, we opt to disable them by default for the sake of
    # consistency and so that we don't have to chase silent failures.
    #
    # [1]: https://sourceware.org/gdb/onlinedocs/gdb/Set-Watchpoints.html
    if disable_hardware_whatchpoints:
        gdb.execute("set can-use-hw-watchpoints 0")
        print("Hardware watchpoints have been disabled. Please do not turn them back on until")
        print("GOT tracking is disabled, as it may lead to unexpected silent errors.")
        print()
        print("They may be re-enabled with `set can-use-hw-watchpoints 1`")
        print()
    else:
        print(
            message.warn("Hardware watchpoints have not been disabled, silent errors may happen.")
        )
        print()

    global GOT_TRACKING
    assert len(INSTALLED_WATCHPOINTS) == 0

    GOT_TRACKING = True

    pwndbg.gdblib.dynamic.r_debug_install_link_map_changed_hook()
    _update_watchpoints()


def disable_got_call_tracking():
    """
    Disable the analysis of calls made through the GOT.
    """
    global GOT_TRACKING
    GOT_TRACKING = False

    for _, (tracker, patcher) in INSTALLED_WATCHPOINTS.items():
        patcher.delete()
        tracker.delete()
    INSTALLED_WATCHPOINTS.clear()
    TRAP_ALLOCATOR.clear()


def jump_slots_for(dynamic):
    """
    Returns the jump slot addresses described by the given dynamic section.
    """
    jump_slots = JUMP_SLOTS[pwndbg.gdblib.arch.name]
    if dynamic.has_rel:
        for i in range(dynamic.rel_entry_count()):
            if dynamic.rel_read(i, "r_type") in jump_slots:
                yield (0, i, dynamic.load_bias + dynamic.rel_read(i, "r_offset"))
    if dynamic.has_rela:
        for i in range(dynamic.rela_entry_count()):
            if dynamic.rela_read(i, "r_type") in jump_slots:
                yield (
                    1,
                    i,
                    dynamic.load_bias
                    + dynamic.rela_read(i, "r_offset")
                    + dynamic.rela_read(i, "r_addend"),
                )
    if dynamic.has_jmprel:
        for i in range(dynamic.jmprel_entry_count()):
            if dynamic.jmprel_read(i, "r_type") in jump_slots:
                base = dynamic.load_bias + dynamic.jmprel_read(i, "r_offset")
                if dynamic.jmprel_elem.has_field("r_addend"):
                    base += dynamic.jmprel_read(i, "r_addend")
                yield (2, i, base)
