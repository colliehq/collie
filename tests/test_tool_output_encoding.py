"""The bash tool reads each line in the encoding it was written in.

On Windows a command's output mixes encodings: git, Git Bash's tools, node and rg write UTF-8,
Python children and legacy console programs write the ANSI code page. Read in the ANSI code
page, every UTF-8 line reached the model garbled wherever that code page is not UTF-8 (936, the
Chinese default). Where it is UTF-8 (65001) the pipes keep the platform default.
"""
import codecs
import io
import sys

import pytest

from harness import tool_process as tp

ZH = "提交说明：修复登录"


def _decode(chunks, ansi, monkeypatch):
    monkeypatch.setattr(tp, "_ansi", lambda: ansi)
    d = codecs.getincrementaldecoder(tp.OUTPUT_CODEC)("replace")
    out = "".join(d.decode(c) for c in chunks)
    return out + d.decode(b"", final=True)


def test_each_line_is_read_in_its_own_encoding(monkeypatch):
    raw = (ZH + " (git, UTF-8)\n").encode("utf-8") + (ZH + " (python, GBK)\n").encode("gbk")
    assert _decode([raw], "gbk", monkeypatch) == ZH + " (git, UTF-8)\n" + ZH + " (python, GBK)\n"


def test_a_character_split_across_reads_is_kept_whole(monkeypatch):
    raw = (ZH + "\n").encode("utf-8")
    chunks = [raw[i:i + 1] for i in range(len(raw))]           # one byte per read
    assert _decode(chunks, "gbk", monkeypatch) == ZH + "\n"


def test_output_without_a_final_newline_is_not_lost(monkeypatch):
    assert _decode([ZH.encode("utf-8")], "gbk", monkeypatch) == ZH
    assert _decode([ZH.encode("gbk")], "gbk", monkeypatch) == ZH


def test_one_huge_line_is_released_in_pieces(monkeypatch):
    monkeypatch.setattr(tp, "_ansi", lambda: "gbk")
    d = codecs.getincrementaldecoder(tp.OUTPUT_CODEC)("replace")
    line = (ZH * 20000).encode("utf-8")                      # ~540 KB, no newline
    got, step = [], 8192                                      # the size TextIOWrapper reads
    for i in range(0, len(line), step):
        got.append(d.decode(line[i:i + step]))
        assert len(d.getstate()[0]) < tp._LONG_LINE_BYTES + step    # memory stays bounded
    got.append(d.decode(b"", final=True))
    assert "".join(got) == ZH * 20000


def test_through_a_text_stream_like_the_pipes(monkeypatch):
    monkeypatch.setattr(tp, "_ansi", lambda: "gbk")
    raw = ("a\r\n" + ZH + "\r\n").encode("utf-8") + ("b " + ZH + "\n").encode("gbk")
    stream = io.TextIOWrapper(io.BytesIO(raw), encoding=tp.OUTPUT_CODEC, errors="replace")
    assert [stream.readline(tp.PIPE_READ_CHARS) for _ in range(4)] == \
        ["a\n", ZH + "\n", "b " + ZH + "\n", ""]


def test_only_windows_with_a_non_utf8_code_page_gets_it(monkeypatch):
    monkeypatch.setattr(tp.plat, "is_windows", lambda: True)
    monkeypatch.setattr(tp, "_ansi", lambda: "cp936")
    assert tp._output_encoding() == tp.OUTPUT_CODEC
    monkeypatch.setattr(tp, "_ansi", lambda: "cp65001")
    assert tp._output_encoding() is None
    monkeypatch.setattr(tp.plat, "is_windows", lambda: False)
    monkeypatch.setattr(tp, "_ansi", lambda: "cp936")
    assert tp._output_encoding() is None


def test_a_real_command_mixing_both(monkeypatch, tmp_path):
    # Forced on, with GBK standing in for the ANSI code page this machine may not have.
    monkeypatch.setattr(tp, "_ansi", lambda: "gbk")
    monkeypatch.setattr(tp, "_output_encoding", lambda: tp.OUTPUT_CODEC)
    script = tmp_path / "mixed.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(%r)\n"
        "sys.stdout.buffer.write(%r)\n"
        "sys.stderr.buffer.write(%r)\n"
        % ((ZH + " utf8\n").encode("utf-8"), (ZH + " gbk\n").encode("gbk"),
           (ZH + " err\n").encode("gbk")), encoding="utf-8")
    out = tp.run_owned([sys.executable, str(script)], use_shell=False, cwd=str(tmp_path),
                       timeout_s=60)
    assert out.status == tp.OK, out
    assert out.stdout.replace("\r\n", "\n") == ZH + " utf8\n" + ZH + " gbk\n"
    assert out.stderr.replace("\r\n", "\n") == ZH + " err\n"


def test_a_huge_line_in_a_double_byte_code_page_stays_paired(monkeypatch):
    # Past 64 KB a line is released in pieces; a GBK lead byte left at a cut became U+FFFD and
    # paired every later byte of the line with the wrong neighbour (7233 of 40002 characters).
    monkeypatch.setattr(tp, "_ansi", lambda: "gbk")
    d = codecs.getincrementaldecoder(tp.OUTPUT_CODEC)("replace")
    line = ("中" * 20001 + "兄" * 20001).encode("gbk") + b"\n"
    got = [d.decode(line[i:i + 8191]) for i in range(0, len(line), 8191)]
    got.append(d.decode(b"", final=True))
    assert "".join(got) == "中" * 20001 + "兄" * 20001 + "\n"


def test_the_rest_of_a_long_line_keeps_the_encoding_it_started_in(monkeypatch):
    # After a cut, a GBK tail such as b"\xd2\xbb tail" is also valid UTF-8 (U+04BB); judged
    # afresh it came back Cyrillic. A line released in pieces is read in one encoding.
    monkeypatch.setattr(tp, "_ansi", lambda: "gbk")
    d = codecs.getincrementaldecoder(tp.OUTPUT_CODEC)("replace")
    head = ("中" * (tp._LONG_LINE_BYTES // 2)).encode("gbk")      # one full piece of GBK
    line = head + "一 tail\n".encode("gbk")
    got = d.decode(line[:len(head) + 1]) + d.decode(line[len(head) + 1:])
    assert got == "中" * (tp._LONG_LINE_BYTES // 2) + "一 tail\n"
