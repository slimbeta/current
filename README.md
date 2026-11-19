# Current

An iPhone-installable reader for the energy transition. Same static site + hourly GitHub Action pattern
as The Commonplace, Degrees and Jus Cogens.

- Topic tabs (Power, Transport, Industry, China, Policy) are keyword tags applied to every story;
  Analysis is a source type (newsletters, data shops and agencies). Configure in `scripts/update.py`
  (`SOURCES`, `TAGS`, `THEMES`).
- Header chart: world electricity mix since 1985 (Ember and Energy Institute via Our World in Data),
  refreshed each run into `data/mix.json`.
- The front page opens with "The week in energy", rebuilt daily from the past 7 days.
