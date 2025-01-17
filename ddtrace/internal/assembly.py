# Grammar:
#
# ident                 ::= [a-zA-Z_][a-zA-Z0-9_]*
# number                ::= [0-9]+
# label                 ::= ident ":"
# label_ref             ::= "@" ident
# string_ref            ::= "$" ident
# try_block_begin       ::= "try" label_ref ["lasti"]?
# try_block_end         ::= "tried"
# opcode                ::= [A-Z][A-Z0-9_]*
# bind_opcode_arg       ::= "{" ident "}"
# opcode_arg            ::= label_ref | string | number | bind_opcode_arg | <Python expression>
# instruction           ::= opcode [opcode_arg]?
# line                  ::= label | try_block_begin | try_block_end | instruction

import dis
import sys
from types import CodeType
import typing as t

import bytecode as bc


if sys.version_info >= (3, 11):
    ParsedLine = t.Union[bc.Instr, bc.Label, bc.TryBegin, bc.TryEnd]
else:
    ParsedLine = t.Union[bc.Instr, bc.Label]


def relocate(instrs: bc.Bytecode, lineno: int) -> bc.Bytecode:
    new_instrs = bc.Bytecode()
    for i in instrs:
        if isinstance(i, bc.Instr):
            new_i = i.copy()
            new_i.lineno = lineno
            new_instrs.append(new_i)
        else:
            new_instrs.append(i)
    return new_instrs


def transform_instruction(opcode: str, arg: t.Any) -> t.Tuple[str, t.Any]:
    # Handle pseudo-instructions
    if sys.version_info >= (3, 12):
        if opcode.upper() == "LOAD_METHOD":
            opcode = "LOAD_ATTR"
            arg = (True, arg)
        elif opcode.upper() == "LOAD_ATTR" and not isinstance(arg, tuple):
            arg = (False, arg)

    return opcode, arg


class BindOpArg(bc.Label):
    # We cannot have arbitrary objects in Bytecode, so we subclass Label
    def __init__(self, name: str, arg: str, lineno: t.Optional[int] = None) -> None:
        self.name = name
        self.arg = arg
        self.lineno = lineno

    def __call__(self, bind_args: t.Dict[str, t.Any], lineno: t.Optional[int] = None) -> bc.Instr:
        return bc.Instr(self.name, bind_args[self.arg], lineno=lineno if lineno is not None else self.lineno)


class Assembly:
    def __init__(
        self, name: t.Optional[str] = None, filename: t.Optional[str] = None, lineno: t.Optional[int] = None
    ) -> None:
        self._labels: t.Dict[str, bc.Label] = {}
        self._ref_labels: t.Dict[str, bc.Label] = {}
        self._tb: t.Optional[bc.TryBegin] = None
        self._instrs = bc.Bytecode()
        self._instrs.name = name or "<assembly>"
        self._instrs.filename = filename or __file__
        self._lineno = lineno
        self._bind_opargs: t.Dict[int, BindOpArg] = {}

    def parse_ident(self, text: str) -> str:
        if not text.isidentifier():
            raise ValueError("invalid identifier %s" % text)

        return text

    def parse_number(self, text: str) -> t.Optional[int]:
        try:
            return int(text)
        except ValueError:
            return None

    def parse_label(self, line: str) -> t.Optional[bc.Label]:
        if not line.endswith(":"):
            return None

        label_ident = self.parse_ident(line[:-1])
        if label_ident in self._labels:
            raise ValueError("label %s already defined" % label_ident)

        label = self._labels[label_ident] = self._ref_labels.pop(label_ident, None) or bc.Label()

        return label

    def parse_label_ref(self, text: str) -> t.Optional[bc.Label]:
        if not text.startswith("@"):
            return None

        label_ident = self.parse_ident(text[1:])

        try:
            return self._labels[label_ident]
        except KeyError:
            try:
                return self._ref_labels[label_ident]
            except KeyError:
                label = self._ref_labels[label_ident] = bc.Label()
                return label

    def parse_string_ref(self, text: str) -> t.Optional[str]:
        if not text.startswith("$"):
            return None

        return self.parse_ident(text[1:])

    if sys.version_info >= (3, 11):

        def parse_try_begin(self, line: str) -> t.Optional[bc.TryBegin]:
            try:
                head, label_ref, *lasti = line.split(maxsplit=2)
            except ValueError:
                return None

            if head != "try":
                return None

            if self._tb is not None:
                raise ValueError("cannot start try block while another is open")

            label = self.parse_label_ref(label_ref)
            if label is None:
                raise ValueError("invalid label reference for try block")

            tb = self._tb = bc.TryBegin(label, push_lasti=bool(lasti))

            return tb

        def parse_try_end(self, line: str) -> t.Optional[bc.TryEnd]:
            if line != "tried":
                return None

            if self._tb is None:
                raise ValueError("cannot end try block while none is open")

            end = bc.TryEnd(self._tb)

            self._tb = None

            return end

    def parse_opcode(self, text: str) -> str:
        opcode = text.upper()
        if opcode not in dis.opmap:
            raise ValueError("unknown opcode %s" % opcode)

        return opcode

    def parse_expr(self, text: str) -> t.Any:
        frame = sys._getframe(1)

        _globals = frame.f_globals.copy()
        _globals["asm"] = bc

        return eval(text, _globals, frame.f_locals)  # nosec

    def parse_opcode_arg(self, text: str) -> t.Union[bc.Label, str, int, t.Any]:
        if not text:
            return bc.UNSET

        return (
            self.parse_label_ref(text)
            or self.parse_string_ref(text)
            or self.parse_number(text)
            or self.parse_expr(text)
        )

    def parse_bind_opcode_arg(self, text: str) -> t.Optional[str]:
        if not text.startswith("{") or not text.endswith("}"):
            return None

        return text[1:-1]

    def parse_instruction(self, line: str) -> t.Optional[t.Union[bc.Instr, BindOpArg]]:
        opcode, *args = line.split(maxsplit=1)

        arg = ""
        if args:
            (arg,) = args
            bind_arg = self.parse_bind_opcode_arg(arg)
            if bind_arg is not None:
                return BindOpArg(self.parse_opcode(opcode), bind_arg, lineno=self._lineno)

        return bc.Instr(
            *transform_instruction(self.parse_opcode(opcode), self.parse_opcode_arg(arg)), lineno=self._lineno
        )

    def parse_line(self, line: str) -> ParsedLine:
        if sys.version_info >= (3, 11):
            entry = (
                self.parse_label(line)
                or self.parse_try_begin(line)
                or self.parse_try_end(line)
                or self.parse_instruction(line)
            )
        else:
            entry = self.parse_label(line) or self.parse_instruction(line)

        if entry is None:
            raise ValueError("invalid line %s" % line)

        return entry

    def _validate(self) -> None:
        if self._ref_labels:
            raise ValueError("undefined labels: %s" % ", ".join(self._ref_labels))

    def parse(self, asm: str) -> None:
        for line in (_.strip() for _ in asm.splitlines()):
            if not line or line.startswith("#"):
                continue

            entry = self.parse_line(line)
            if isinstance(entry, BindOpArg):
                self._bind_opargs[len(self._instrs)] = entry

            self._instrs.append(entry)

        self._validate()

    def bind(self, bind_args: t.Optional[t.Dict[str, t.Any]] = None, lineno: t.Optional[int] = None) -> bc.Bytecode:
        if not self._bind_opargs:
            if lineno is not None:
                return relocate(self._instrs, lineno)
            return self._instrs

        if bind_args is None:
            raise ValueError("missing bind arguments")

        # If we have bind opargs, the bytecode we parsed has some
        # BindOpArg placeholders that need to be resolved. Therefore, we
        # make a copy of the parsed bytecode and replace the BindOpArg
        # placeholders with the resolved values.
        instrs = bc.Bytecode(self._instrs)
        for i, arg in self._bind_opargs.items():
            instrs[i] = arg(bind_args, lineno=lineno)

        return relocate(instrs, lineno) if lineno is not None else instrs

    def compile(self, bind_args: t.Optional[t.Dict[str, t.Any]] = None, lineno: t.Optional[int] = None) -> CodeType:
        return self.bind(bind_args, lineno=lineno).to_code()

    def _label_ident(self, label: bc.Label) -> str:
        return next(ident for ident, l in self._labels.items() if l is label)

    def dis(self) -> None:
        for entry in self._instrs:
            if isinstance(entry, bc.Instr):
                print(f"    {entry.name:<32}{entry.arg if entry.arg is not None else ''}")
            elif isinstance(entry, BindOpArg):
                print(f"    {entry.name:<32}{{{entry.arg}}}")
            elif isinstance(entry, bc.Label):
                print(f"{self._label_ident(entry)}:")
            elif isinstance(entry, bc.TryBegin):
                print(f"try @{self._label_ident(entry.target)} (lasti={entry.push_lasti})")

    def __iter__(self) -> t.Iterator[bc.Instr]:
        return iter(self._instrs)
