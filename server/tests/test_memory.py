from agatha_server.memory import MemoryConfig, MemoryStore
from agatha_server.prompts import memory_block, system_message
from agatha_server.tools.memory import memory_tools


def store(**kwargs) -> MemoryStore:
    return MemoryStore(MemoryConfig(db=":memory:", **kwargs))


def test_lembra_lista_e_esquece(tmp_path):
    db = str(tmp_path / "m" / "agatha.db")
    s = MemoryStore(MemoryConfig(db=db))
    fact, status = s.add("O Reinaldo usa o padrão ambiente-servidor-host no Zabbix")
    assert status == "novo" and fact.id == 1
    s.add("O banco de produção se chama PRDORA")
    s.close()
    s = MemoryStore(MemoryConfig(db=db))  # persiste entre reinícios
    assert [f.text for f in s.facts()] == [
        "O Reinaldo usa o padrão ambiente-servidor-host no Zabbix", "O banco de produção se chama PRDORA"]
    assert s.forget(1).text.startswith("O Reinaldo usa")
    assert s.forget(1) is None
    assert [f.id for f in s.facts()] == [2]


def test_repetido_vazio_longo_e_cheio():
    s = store(max_facts=2, max_chars=30)
    s.add("Notebook se chama rei-note.")
    assert s.add("notebook se chama REI-NOTE")[1] == "repetido"  # sem caixa/pontuação
    assert s.add("   ")[1] == "vazio"
    assert s.add("x" * 31)[1] == "longo"
    s.add("Reunião na quinta")
    assert s.add("Mais um fato")[1] == "cheio"


def test_versao_muda_a_cada_alteracao():
    s = store()
    v0 = s.version
    s.add("fato")
    s.add("fato")  # repetido: não muda
    assert s.version == v0 + 1
    s.forget(1)
    assert s.version == v0 + 2


def test_ferramentas():
    s = store()
    guardar, esquecer = memory_tools(s)
    assert guardar.handler({"fato": "Reunião na quinta às 10h"}) == {"ok": True, "numero": 1}
    assert guardar.handler({"fato": "reunião na quinta às 10h"})["ja_sabia"] is True
    assert "erro" in guardar.handler({"fato": ""})
    assert esquecer.handler({"numero": 1}) == {"ok": True, "esquecido": "Reunião na quinta às 10h"}
    assert "não existe" in esquecer.handler({"numero": 1})["erro"]
    assert "número" in esquecer.handler({"numero": "aquilo"})["erro"]


def test_bloco_de_memoria_no_prompt():
    s = store()
    assert "vazia" in memory_block(s.facts())
    s.add("O banco de produção se chama PRDORA")
    assert "1. O banco de produção se chama PRDORA" in system_message(None, s.facts())["content"]
    assert "memória" not in system_message(None, None)["content"].lower()  # memória desligada
