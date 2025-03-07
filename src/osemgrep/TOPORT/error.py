from semgrep.rule_lang import SourceTracker
from semgrep.rule_lang import Span

class SemgrepError(Exception):

    def to_dict(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.to_CliError().to_json())

    def format_for_terminal(self) -> str:
        level_tag = (
            with_color(Colors.red, "[", bgcolor=Colors.red)
            + with_color(
                Colors.forced_white, self.level.name, bgcolor=Colors.red, bold=True
            )
            + with_color(Colors.red, "]", bgcolor=Colors.red)
        )

        return f"{level_tag} {self}"

    def semgrep_error_type(self) -> str:
        return type(self).__name__


class SemgrepCoreError(SemgrepError):
    def is_timeout(self) -> bool:
        """
        Return if this error is a match timeout
        """
        return isinstance(self.core.error_type.value, core.Timeout)

    def semgrep_error_type(self) -> str:
        return f"{type(self).__name__}: {self._error_type_string()}"

    @property
    def _stack_trace(self) -> str:
        """
        Returns stack trace if error_type is Fatal error else returns empty strings
        """
        if isinstance(self.core.error_type.value, core.FatalError):
            error_trace = self.core.details or "<no stack trace returned>"
            return f"\n====[ BEGIN error trace ]====\n{error_trace}=====[ END error trace ]=====\n"
        else:
            return ""

    def __str__(self) -> str:
        return self._error_message + self._stack_trace

    # TODO: I didn't manage to get core.Error to be hashable because it contains lists or
    # objects (e.g., Error_) which are not hashable
    def __hash__(self) -> int:
        return hash(
            (
                self.code,
                self.level,
                self.core.rule_id,
                self.core.error_type.kind,
                self.core.location.path,
                self.core.location.start,
                self.core.location.end,
                self.core.message,
                self.core.details,
            )
        )


class SemgrepInternalError(Exception):
    """
    Parent class of internal semgrep exceptions that should be handled internally and converted into `SemgrepError`s

    Classes that inherit from SemgrepInternalError should begin with `_`
    """


class FilesNotFoundError(SemgrepError):
    level = Level.ERROR
    code = FATAL_EXIT_CODE
    paths: Sequence[Path]

    def __str__(self) -> str:
        lines = (f"File not found: {self.paths}" for pathname in self.paths)
        return "\n".join(lines)


def span_list_to_tuple(spans: List[Span]) -> Tuple[Span, ...]:
    """
    Helper converter so mypy can track that we are converting
    from list of spans to tuple of spans
    """
    return tuple(spans)


class ErrorWithSpan(SemgrepError):
    """
    """
    def __attrs_post_init__(self) -> None:
        if not hasattr(self, "code"):
            raise ValueError("Inheritors of SemgrepError must define an exit code")

        if not hasattr(self, "level"):
            raise ValueError("Inheritors of SemgrepError must define a level")

    def adjust_CliError(self, base: out.CliError) -> out.CliError:
        base = dataclasses.replace(
            base,
            short_msg=self.short_msg,
            long_msg=self.long_msg,
            level=self.level.name.lower(),
            spans=[s.to_ErrorSpan() for s in self.spans],
        )
        # otherwise, we end up with `help: null` in JSON
        if self.help:
            base = dataclasses.replace(base, help=self.help)
        return base

    @staticmethod
    def _line_number_width(span: Span) -> int:
        return len(str((span.context_end or span.end).line)) + 1

    @staticmethod
    def _format_line_number(span: Span, line_number: Optional[int]) -> str:
        """
        Produce a string like:
        ` 10 |`

        The amount of padding is set for printing within `span` (so it handles up to `context_end.line`)
        """
        # line numbers are 0 indexed
        width = ErrorWithSpan._line_number_width(span)
        if line_number is not None:
            base_str = str(line_number)
            assert len(base_str) < width
            return with_color(Colors.bright_blue, base_str.ljust(width) + "| ")
        else:
            return with_color(Colors.bright_blue, "".ljust(width) + "| ")

    def _format_code_segment(
        self, start: Position, end: Position, source: List[str], part_of_span: Span
    ) -> List[str]:
        """
        Line by line output for a snippet of code from `start_line` to `end_line`
        Each line will be annotated with a line number, properly spaced according to
        the highest line number required to render `span`

        :param start: start position
        :param end: end position

        :returns A list of strings, suitable to be combined with `'\n'.join(...)`
        eg:
        List[
            "5  | def my_func():",
            "6  |   return True"
        ]
        """
        # -1 because positions are 1-indexed
        code_segment = source[start.line - 1 : end.line]
        snippet = []
        for line_num, line in zip(range(start.line, end.line + 1), code_segment):
            snippet.append(f"{self._format_line_number(part_of_span, line_num)}{line}")
        return snippet

    def __str__(self) -> str:
        return self.short_msg

    def format_for_terminal(self) -> str:
        """
        Format this exception into a pretty string with context and color
        """
        header = f"{with_color(Colors.red, 'semgrep ' + self.level.name.lower())}: {self.short_msg}"
        snippets = []
        for span in self.spans:
            if span.file != "semgrep temp file":
                location_hint = f"  --> {span.file}:{span.start.line}"
                snippet = [location_hint]
            else:
                snippet = []

            # all the lines of code in the file this comes from
            source: List[str] = SourceTracker.source(span.source_hash)

            # First, print the span from `context_start` to `start`
            # Next, sprint the focus of the span from `start` to `end`
            # If the actual span is only 1 line long, use `column` information to highlight the exact problem
            # Finally, print end context from `end` to `context_end`
            if span.context_start:
                snippet += self._format_code_segment(
                    span.context_start, span.start.previous_line(), source, span
                )
            snippet += self._format_code_segment(span.start, span.end, source, span)
            # Currently, only span highlighting if it's a one line span
            if span.start.line == span.end.line:
                error = with_color(Colors.red, (span.end.col - span.start.col) * "^")
                snippet.append(
                    self._format_line_number(span, None)
                    + " " * (span.start.col - 1)
                    + error
                )
            if span.context_end:
                snippet += self._format_code_segment(
                    span.end.next_line(), span.context_end, source, span
                )

            snippets.append("\n".join(snippet))
        snippet_str = "\n".join(snippets)
        if self.help:
            help_str = f"= {with_color(Colors.cyan, 'help', bold=True)}: {self.help}"
        else:
            help_str = ""

        # TODO remove this when temp files are no longer in error messages
        if snippet_str == "":
            snippet_str_with_newline = ""
        else:
            snippet_str_with_newline = f"{snippet_str}\n"
        return f"{header}\n{snippet_str_with_newline}{help_str}\n{with_color(Colors.red, self.long_msg or '')}\n"

# cf. https://stackoverflow.com/questions/1796180/how-can-i-get-a-list-of-all-classes-within-current-module-in-python/1796247#1796247
ERROR_MAP = {
    classname: classdef
    for classname, classdef in inspect.getmembers(
        sys.modules[__name__],
        lambda member: inspect.isclass(member) and member.__module__ == __name__,
    )
}
