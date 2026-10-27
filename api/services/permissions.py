"""O que cada papel pode fazer num projeto, editável pelo superusuário.

Antes, cada rota fixava o papel mínimo no próprio `require_role("engineer")`,
e mudar quem pode o quê era editar código. Agora a rota declara a **ação**
(`require_action("datasets.versoes.congelar")`) e quem decide é esta tabela:

1. **`owner` pode tudo, sempre.** Não entra na matriz nem na tabela (o CHECK
   da migration 018 recusa). Um projeto sem ninguém capaz de gerir membros ou
   arquivar não tem saída de dentro do produto.
2. Senão, a exceção que o superusuário gravou em `role_permission`.
3. Senão, o padrão do catálogo — o papel mínimo que a rota exigia antes desta
   tabela existir, para que a migração não mude o comportamento de ninguém.

O que NÃO está aqui, de propósito: a leitura (ser membro já é poder ver, e é
isso que `require_role("viewer")` continua conferindo), o que é da instalação
(`require_superuser`) e as guardas de owner de `services/users.py` — conceder
`projeto.membros.gerenciar` a um papel não deixa ninguém rebaixar ou remover owner
sem ser owner.

Sem cache: a decisão já é uma consulta por requisição (junto com projeto,
papel e arquivamento), e revogar tem que valer na próxima requisição, não
dez segundos depois.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from .. import db
from ..audit import audit
from ..auth import Principal

# Papéis são lineares: quem pode mais pode menos. É a régua do padrão de cada
# ação e da guarda de owner em `services/users.py`.
ROLE_RANK = {"viewer": 0, "annotator": 1, "engineer": 2, "admin": 3, "owner": 4}

# Os que a matriz configura: todos menos owner.
CONFIGURAVEIS = ("viewer", "annotator", "engineer", "admin")

NOME_PAPEL = {"viewer": "Visualizador", "annotator": "Anotador",
              "engineer": "Engenheiro", "admin": "Administrador",
              "owner": "Dono"}


@dataclass(frozen=True)
class Acao:
    """Uma ação da tela. A chave é `página.seção.ação` — a página e a seção
    são as da interface, para que quem edita a matriz ache a ação onde a usa.
    Os nomes de exibição vêm separados porque a chave não pode mudar (está
    gravada em `role_permission`) e o texto da tela pode."""
    chave: str
    pagina: str
    secao: str
    nome: str
    ajuda: str
    padrao: str
    # Conceder abaixo do padrão abre escalada: quem gere membros se promove.
    sensivel: bool = False


# A ordem é a da tela: páginas na ordem das abas, seções na ordem da página.
ACOES: dict[str, Acao] = {a.chave: a for a in (
    Acao("projeto.geral.editar", "Projeto", "Geral", "Editar projeto",
         "Nome e descrição.", "admin"),
    Acao("projeto.geral.arquivar", "Projeto", "Geral", "Arquivar projeto",
         "Some das listagens e passa a só leitura.", "owner"),
    Acao("projeto.classes.acrescentar", "Projeto", "Classes",
         "Acrescentar classes",
         "Nova versão do vocabulário, só acrescentando no fim.", "engineer"),
    Acao("projeto.membros.gerenciar", "Projeto", "Membros",
         "Gerenciar membros",
         "Incluir, trocar o papel e remover membros, até o próprio papel. "
         "Mexer em owner continua exigindo owner.", "admin", sensivel=True),
    Acao("datasets.lista.criar", "Datasets", "Lista", "Criar dataset",
         "Cadastrar um dataset novo no projeto.", "engineer"),
    Acao("datasets.origens.navegar", "Datasets", "Origens",
         "Navegar buckets",
         "Listar e percorrer os buckets permitidos da instalação.",
         "engineer"),
    Acao("datasets.origens.importar", "Datasets", "Origens",
         "Importar imagens",
         "Importar do bucket ou de um dump do Label Studio.", "engineer"),
    Acao("datasets.origens.editar", "Datasets", "Origens",
         "Incluir e retirar origens",
         "Retirar uma origem apaga as imagens que só ela trazia.",
         "engineer"),
    Acao("datasets.recorte.editar", "Datasets", "Recorte (ROI)",
         "Ajustar recorte", "Definir ou remover a ROI do dataset.",
         "engineer"),
    Acao("datasets.versoes.congelar", "Datasets", "Versões",
         "Congelar versão", "Gera a versão imutável e sorteia o split.",
         "engineer"),
    Acao("datasets.prelabel.descartar", "Datasets", "Pré-tag",
         "Descartar pré-tags", "Apaga os pré-tags ainda não revisados.",
         "engineer"),
    Acao("rotulagem.anotador.salvar", "Rotulagem", "Anotador",
         "Salvar anotação", "Gravar a própria anotação de uma imagem.",
         "annotator"),
    Acao("rotulagem.anotador.gabarito", "Rotulagem", "Anotador",
         "Editar gabarito de corte",
         "Gabarito de cortes de uma classe principal (segmentação).",
         "annotator"),
    Acao("rotulagem.prelabel.executar", "Rotulagem", "Pré-tag",
         "Pré-taguear", "Enfileirar pré-tag com um modelo do projeto.",
         "engineer"),
    Acao("treino.runs.criar", "Treinamento", "Treinos", "Disparar treino",
         "Enfileirar um treino numa máquina.", "engineer"),
    Acao("treino.runs.cancelar", "Treinamento", "Treinos", "Cancelar treino",
         "Na fila cancela na hora; rodando, no próximo heartbeat.",
         "engineer"),
    Acao("modelos.versoes.promover", "Modelos", "Versões",
         "Promover versão", "Pôr e tirar alias (champion, candidate…).",
         "engineer"),
    Acao("modelos.versoes.avaliar", "Modelos", "Versões", "Avaliar versão",
         "Medir uma versão contra um dataset.", "engineer"),
    Acao("modelos.versoes.baixar", "Modelos", "Versões", "Baixar peso",
         "Download do arquivo de pesos de uma versão.", "engineer"),
    Acao("modelos.versoes.exportar", "Modelos", "Versões",
         "Compilar para borda",
         "Gerar o artefato de uma versão para um dispositivo.", "engineer"),
    Acao("modelos.implantacao.implantar", "Modelos", "Implantação",
         "Implantar", "Mandar um build para um dispositivo de borda.",
         "engineer"),
)}


def permitido(papel: str, acao: str, excecao: bool | None) -> bool:
    if papel == "owner":
        return True
    if excecao is not None:
        return excecao
    return ROLE_RANK[papel] >= ROLE_RANK[ACOES[acao].padrao]


async def decidir(conn: db.Conn, project_slug: str, user: Principal,
                  acao: str) -> None:
    """Levanta o erro certo se `user` não pode `acao` no projeto; senão nada.

    Uma consulta só para projeto, papel, arquivamento e exceção. A ordem dos
    erros é a de `require_role`: não-membro é 404 e não 403 (403 revelaria que
    o projeto existe), arquivado é 409 antes do papel, e só então 403.
    """
    row = await db.fetch_one(
        "SELECT p.archived_at, m.role, rp.allowed"
        "  FROM project p"
        "  LEFT JOIN project_member m"
        "    ON m.project_id = p.id AND m.user_id = %(u)s"
        "  LEFT JOIN role_permission rp"
        "    ON rp.role = m.role AND rp.action = %(a)s"
        " WHERE p.slug = %(s)s",
        {"u": user.id, "a": acao, "s": project_slug}, conn=conn)
    if row is None or (row["role"] is None and not user.is_superuser):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "projeto não encontrado")
    if row["archived_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "projeto arquivado — só leitura")
    papel = "owner" if user.is_superuser else row["role"]
    if not permitido(papel, acao, row["allowed"]):
        a = ACOES[acao]
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"seu papel neste projeto ({NOME_PAPEL[papel]}) não permite: "
            f"{a.nome.lower()}")


async def efetivas(conn: db.Conn, papel: str | None) -> list[str]:
    """As ações que `papel` pode, na ordem do catálogo — o que a tela usa para
    mostrar ou esconder cada botão."""
    if papel is None:
        return []
    if papel == "owner":
        return list(ACOES)
    rows = await db.fetch_all(
        "SELECT action, allowed FROM role_permission WHERE role = %s",
        (papel,), conn=conn)
    exc = {r["action"]: r["allowed"] for r in rows}
    return [k for k in ACOES if permitido(papel, k, exc.get(k))]


def _linha(a: Acao, exc: dict[tuple[str, str], bool]) -> dict[str, Any]:
    papeis = {}
    for p in CONFIGURAVEIS:
        e = exc.get((p, a.chave))
        papeis[p] = {"permitido": permitido(p, a.chave, e),
                     "alterado": e is not None}
    return {"chave": a.chave, "pagina": a.pagina, "secao": a.secao,
            "nome": a.nome,
            "ajuda": a.ajuda, "padrao": a.padrao, "sensivel": a.sensivel,
            "papeis": papeis}


async def _excecoes(conn: db.Conn) -> dict[tuple[str, str], bool]:
    rows = await db.fetch_all(
        "SELECT role, action, allowed FROM role_permission", conn=conn)
    return {(r["role"], r["action"]): r["allowed"] for r in rows}


async def matriz() -> dict[str, Any]:
    async with db.connection() as conn:
        exc = await _excecoes(conn)
    return {"papeis": [{"chave": p, "nome": NOME_PAPEL[p]}
                       for p in CONFIGURAVEIS],
            "acoes": [_linha(a, exc) for a in ACOES.values()]}


def _alvo(acao: str, papel: str) -> Acao:
    a = ACOES.get(acao)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ação desconhecida")
    if papel not in CONFIGURAVEIS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "papel desconhecido" if papel not in ROLE_RANK
            else "o dono do projeto pode tudo — não é configurável")
    return a


async def definir(admin: Principal, acao: str, papel: str,
                  valor: bool | None, ip: str | None) -> dict[str, Any]:
    """Grava a exceção, ou a apaga quando `valor` é `None` ou igual ao padrão.

    Igual ao padrão vira ausência de linha, e não linha redundante: senão uma
    mudança futura do padrão no código não chegaria a quem só tinha marcado e
    desmarcado a caixa.
    """
    a = _alvo(acao, papel)
    padrao = permitido(papel, acao, None)
    async with db.connection() as conn, conn.transaction():
        # Pelo mesmo motivo de `parameters.definir`: na primeira gravação não
        # há linha para `FOR UPDATE`, e o lock é o que faz o `before` da
        # trilha dizer a verdade sob duas gravações simultâneas.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                           (f"permission:{papel}:{acao}",))
        antes = await db.fetch_one(
            "SELECT allowed FROM role_permission"
            " WHERE role = %s AND action = %s", (papel, acao), conn=conn)
        if valor is None or valor == padrao:
            await db.execute(
                "DELETE FROM role_permission WHERE role = %s AND action = %s",
                (papel, acao), conn=conn)
        else:
            await db.execute(
                "INSERT INTO role_permission (role, action, allowed,"
                " updated_by) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (role, action) DO UPDATE"
                "   SET allowed = EXCLUDED.allowed, updated_at = now(),"
                "       updated_by = EXCLUDED.updated_by",
                (papel, acao, valor, admin.id), conn=conn)
        era = antes["allowed"] if antes else padrao
        fica = padrao if valor is None else valor
        if era != fica:
            await audit(admin, "permission.set", "role_permission", None,
                        before={"role": papel, "action": acao, "allowed": era},
                        after={"role": papel, "action": acao, "allowed": fica},
                        ip=ip, conn=conn)
        exc = await _excecoes(conn)
    return _linha(a, exc)
