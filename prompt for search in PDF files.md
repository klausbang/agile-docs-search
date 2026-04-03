Make a new python program with a GUI. Make the GUI as a web page. 

The program shall search a directory and subdirectories for PDF files. Input field: File browser for selection of directory to search in. Make the ./downloads folder be the default folder.

In the PDF files the program shall search for words and sentences defined by the user. Input field: Multiline text control, containing the words and sentenses that shall be searched for in the PDF files.

For each occurence of the search findings, the following shall be shown:
* Document name and a link to the document.
* Locations within PDF where found.
* Text snippet from the PDF showing context of the found words or sentence, i.e. text before and after found text.
* In the text snippet, the found word/sentence shall be highlighted

Archive to Git and GitHub: user name klausbang, email: klaus.bang.andersen@gmail.com.
Ignore the downloads folder when archiving.

Updates:
Add search options as normally found in text search functionality, e.g. "only full word", as with search string "demo", "demo", "demo.", "demo," is found, but "demonstrate" is ignored. Sase sensitive. Make the options as check boxes to be selcted/deselected.

Make an additional search out put with limited context - show only the line itself where the search word/sentence is found and add a slider that can extend the limited context to more lines before and after.