"""Camada de serviço: a regra de domínio e o SQL que a implementa.

O router acima só faz o que é de HTTP — rota, corpo validado, RBAC e o código de
status. Quem decide *o que acontece* mora aqui, para que a mesma regra possa ser
chamada de outro lugar (um worker de fila, um script de manutenção) sem passar
por uma requisição.

Duas convenções, escolhidas para não criar cerimônia:

* **O serviço levanta `HTTPException`.** Uma hierarquia paralela de erros de
  domínio só para o router traduzir de volta seria uma segunda descrição do
  mesmo erro. `HTTPException` com texto em português já é o vocabulário de erro
  do repositório.
* **O serviço não conhece pydantic.** Recebe escalares e estruturas simples
  (`dict`, `list[dict]`); é o router que valida o corpo e converte. Assim o
  modelo de entrada da API pode mudar sem arrastar a regra junto.
"""
