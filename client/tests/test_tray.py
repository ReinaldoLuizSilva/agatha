"""Partes do modo bandeja que não precisam de tela: log, estado mostrado, ícones, instância única,
atalho do Windows. O pystray (a bandeja em si) só é importado ao abrir a bandeja."""

import logging

import pytest

from agatha_client.autostart import shortcut_script
from agatha_client.controls import CONNECTING, LISTENING, SPEAKING, Controls
from agatha_client.instance import SingleInstance
from agatha_client.tray import LineLogger, shown_state


def test_print_aos_pedacos_vira_uma_linha_no_log(caplog):
    out = LineLogger(logging.getLogger("teste"), logging.INFO)
    with caplog.at_level(logging.INFO, logger="teste"):
        out.write("  agatha: ")
        out.write("São 18h01. ")
        out.write("\n")
        out.write("\n")  # linhas vazias não poluem o log
        out.write("linha 2\nlinha 3 sem fim")
    assert [r.message for r in caplog.records] == ["  agatha: São 18h01.", "linha 2"]


def test_estado_mostrado_com_o_microfone_pausado():
    c = Controls()
    c.status(LISTENING)
    assert shown_state(c) == LISTENING
    c.muted = True
    assert shown_state(c) == "pausado"
    c.status(CONNECTING)
    assert shown_state(c) == CONNECTING  # sem conexão, isso é o que importa


def test_aviso_de_estado():
    seen = []
    c = Controls(on_status=seen.append)
    c.status(LISTENING)
    c.status(SPEAKING)
    assert seen == [LISTENING, SPEAKING] and c.state == SPEAKING


def test_icones_por_estado():
    pytest.importorskip("PIL")
    from agatha_client.tray import COLORS, icon_image

    for state in COLORS:
        img = icon_image(state)
        assert img.size == (64, 64) and img.getpixel((32, 8))[:3] == COLORS[state]


def test_uma_instancia_so(tmp_path):
    first, second = SingleInstance("AgathaTeste", tmp_path), SingleInstance("AgathaTeste", tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_atalho_do_windows_com_aspas_escapadas(tmp_path):
    script = shortcut_script(tmp_path / "Agatha.lnk", tmp_path / "py" / "pythonw.exe", tmp_path / "d'Ávila")
    assert "WScript.Shell" in script and "-m agatha_client --tray" in script
    assert "d''Ávila" in script  # aspas simples dobradas no PowerShell
