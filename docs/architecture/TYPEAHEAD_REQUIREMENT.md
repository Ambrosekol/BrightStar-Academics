# Crainbow — Type-Ahead/Suggestions Requirement

A previous Crainbow build contains platform-wide typing suggestions/type-ahead behavior that makes data entry fast. This behavior is a retained product requirement.

During architectural refactoring:
- identify the existing implementation and source of suggestions;
- preserve it where correct;
- standardize it through reusable components/services where practical;
- ensure suggestions respect authorization/scope;
- do not leak restricted student, candidate, question-bank or administrative data into suggestion payloads;
- ensure keyboard navigation, accessibility and mobile behavior remain functional.

Regression coverage must verify the feature on representative administrative search/input fields before production sign-off.
